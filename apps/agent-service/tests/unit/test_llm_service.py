"""test_llm_service.py — LLMService 单元测试

场景覆盖：
- run: 返回 AIMessage、传递 callbacks + run_name、瞬时错误重试
- extract: 返回 Pydantic model、传递 model_kwargs
- stream: 返回 AsyncGenerator[AIMessageChunk]
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, SystemMessage
from openai import APITimeoutError
from pydantic import BaseModel

from app.agents.infra.llm_service import LLMService

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_model():
    """Mock BaseChatModel with ainvoke and astream"""
    model = AsyncMock()
    model.ainvoke = AsyncMock(return_value=AIMessage(content="hello"))
    model.with_structured_output = MagicMock()
    return model


@pytest.fixture()
def mock_prompt():
    """Mock Langfuse prompt that returns a compiled string"""
    prompt = MagicMock()
    prompt.compile.return_value = "You are a helpful assistant."
    return prompt


@pytest.fixture()
def mock_deps(mock_model, mock_prompt):
    """Patch ModelBuilder, get_prompt, and CallbackHandler"""
    with (
        patch(
            "app.agents.infra.llm_service.ModelBuilder.build_chat_model",
            new_callable=AsyncMock,
            return_value=mock_model,
        ) as mock_build,
        patch(
            "app.agents.infra.llm_service.get_prompt",
            return_value=mock_prompt,
        ) as mock_get_prompt,
        patch(
            "app.agents.infra.llm_service.CallbackHandler",
            return_value=MagicMock(),
        ) as mock_cb,
    ):
        yield {
            "build_chat_model": mock_build,
            "get_prompt": mock_get_prompt,
            "callback_handler": mock_cb,
            "model": mock_model,
            "prompt": mock_prompt,
        }


# ---------------------------------------------------------------------------
# run() tests
# ---------------------------------------------------------------------------


class TestRun:
    """LLMService.run() 测试"""

    async def test_run_returns_ai_message(self, mock_deps):
        """run 返回 AIMessage"""
        messages = [{"role": "user", "content": "hi"}]
        result = await LLMService.run(
            prompt_id="test-prompt",
            prompt_vars={"name": "test"},
            messages=messages,
            model_id="test-model",
            trace_name="test-trace",
        )

        assert isinstance(result, AIMessage)
        assert result.content == "hello"

    async def test_run_passes_callbacks_to_model(self, mock_deps):
        """run 传递 callbacks 和 run_name 到 model.ainvoke 的 config"""
        messages = [{"role": "user", "content": "hi"}]
        await LLMService.run(
            prompt_id="test-prompt",
            prompt_vars={},
            messages=messages,
            model_id="test-model",
            trace_name="my-trace",
        )

        # ainvoke 应该被调用，检查 config 参数
        call_args = mock_deps["model"].ainvoke.call_args
        config = call_args.kwargs.get("config") or call_args[1].get("config")
        assert config is not None
        assert "callbacks" in config
        assert len(config["callbacks"]) == 1
        assert config["run_name"] == "my-trace"

    async def test_run_builds_system_message_from_prompt(self, mock_deps):
        """run 将 prompt.compile 结果作为 SystemMessage 拼在 messages 前面"""
        user_messages = [{"role": "user", "content": "hello"}]
        await LLMService.run(
            prompt_id="test-prompt",
            prompt_vars={"key": "val"},
            messages=user_messages,
            model_id="test-model",
        )

        # 验证 prompt.compile 被正确调用
        mock_deps["prompt"].compile.assert_called_once_with(key="val")

        # 验证 ainvoke 收到的消息列表
        call_args = mock_deps["model"].ainvoke.call_args
        sent_messages = call_args[0][0]
        assert isinstance(sent_messages[0], SystemMessage)
        assert sent_messages[0].content == "You are a helpful assistant."

    async def test_run_retries_on_transient_error(self, mock_deps):
        """run 在瞬时错误时重试，第二次成功"""
        mock_deps["model"].ainvoke = AsyncMock(
            side_effect=[
                APITimeoutError(request=MagicMock()),
                AIMessage(content="retry success"),
            ]
        )

        result = await LLMService.run(
            prompt_id="test-prompt",
            prompt_vars={},
            messages=[{"role": "user", "content": "hi"}],
            model_id="test-model",
            max_retries=2,
        )

        assert result.content == "retry success"
        assert mock_deps["model"].ainvoke.call_count == 2

    async def test_run_raises_after_max_retries(self, mock_deps):
        """run 超过最大重试次数后抛出异常"""
        mock_deps["model"].ainvoke = AsyncMock(
            side_effect=APITimeoutError(request=MagicMock())
        )

        with pytest.raises(APITimeoutError):
            await LLMService.run(
                prompt_id="test-prompt",
                prompt_vars={},
                messages=[{"role": "user", "content": "hi"}],
                model_id="test-model",
                max_retries=2,
            )

        assert mock_deps["model"].ainvoke.call_count == 2

    async def test_run_no_retry_when_max_retries_is_one(self, mock_deps):
        """max_retries=1 时异常直接抛出不重试"""
        mock_deps["model"].ainvoke = AsyncMock(
            side_effect=APITimeoutError(request=MagicMock())
        )

        with pytest.raises(APITimeoutError):
            await LLMService.run(
                prompt_id="test-prompt",
                prompt_vars={},
                messages=[{"role": "user", "content": "hi"}],
                model_id="test-model",
                max_retries=1,
            )

        assert mock_deps["model"].ainvoke.call_count == 1


# ---------------------------------------------------------------------------
# extract() tests
# ---------------------------------------------------------------------------


class TestExtract:
    """LLMService.extract() 测试"""

    async def test_extract_returns_pydantic_model(self, mock_deps):
        """extract 返回 Pydantic model 实例"""

        class MySchema(BaseModel):
            name: str
            score: float

        expected = MySchema(name="test", score=0.9)
        structured_model = AsyncMock()
        structured_model.ainvoke = AsyncMock(return_value=expected)
        mock_deps["model"].with_structured_output.return_value = structured_model

        result = await LLMService.extract(
            prompt_id="test-prompt",
            prompt_vars={},
            messages=[{"role": "user", "content": "extract this"}],
            schema=MySchema,
            model_id="test-model",
            trace_name="extract-trace",
        )

        assert isinstance(result, MySchema)
        assert result.name == "test"
        assert result.score == 0.9
        mock_deps["model"].with_structured_output.assert_called_once_with(MySchema)

    async def test_extract_passes_model_kwargs(self, mock_deps):
        """extract 将 model_kwargs 传递给 ModelBuilder.build_chat_model"""

        class MySchema(BaseModel):
            value: str

        structured_model = AsyncMock()
        structured_model.ainvoke = AsyncMock(
            return_value=MySchema(value="ok")
        )
        mock_deps["model"].with_structured_output.return_value = structured_model

        await LLMService.extract(
            prompt_id="test-prompt",
            prompt_vars={},
            messages=[],
            schema=MySchema,
            model_id="test-model",
            model_kwargs={"reasoning_effort": "low", "temperature": 0.5},
        )

        mock_deps["build_chat_model"].assert_called_once_with(
            "test-model", reasoning_effort="low", temperature=0.5
        )

    async def test_extract_retries_on_transient_error(self, mock_deps):
        """extract 在瞬时错误时重试"""

        class MySchema(BaseModel):
            value: str

        expected = MySchema(value="ok")
        structured_model = AsyncMock()
        structured_model.ainvoke = AsyncMock(
            side_effect=[
                APITimeoutError(request=MagicMock()),
                expected,
            ]
        )
        mock_deps["model"].with_structured_output.return_value = structured_model

        result = await LLMService.extract(
            prompt_id="test-prompt",
            prompt_vars={},
            messages=[],
            schema=MySchema,
            model_id="test-model",
            max_retries=2,
        )

        assert result.value == "ok"
        assert structured_model.ainvoke.call_count == 2


# ---------------------------------------------------------------------------
# stream() tests
# ---------------------------------------------------------------------------


class TestStream:
    """LLMService.stream() 测试"""

    async def test_stream_yields_chunks(self, mock_deps):
        """stream 返回 AsyncGenerator 并 yield AIMessageChunk"""
        chunks = [
            AIMessageChunk(content="hel"),
            AIMessageChunk(content="lo"),
        ]

        async def fake_astream(messages, *, config=None):
            for chunk in chunks:
                yield chunk

        mock_deps["model"].astream = fake_astream

        collected = []
        async for chunk in LLMService.stream(
            prompt_id="test-prompt",
            prompt_vars={},
            messages=[{"role": "user", "content": "hi"}],
            model_id="test-model",
            trace_name="stream-trace",
        ):
            collected.append(chunk)

        assert len(collected) == 2
        assert collected[0].content == "hel"
        assert collected[1].content == "lo"
