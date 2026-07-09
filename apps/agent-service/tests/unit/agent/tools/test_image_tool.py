from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.agent.context import AgentContext
from app.agent.runtime_context import agent_context
from app.agent.tools import image as image_tool

pytestmark = pytest.mark.unit


async def test_generate_image_exposes_quality_levels_without_model_names():
    schema = image_tool.generate_image.definition.parameters["properties"]

    assert schema["quality"]["enum"] == ["high", "normal"]
    assert "modelhub" not in str(schema).lower()
    assert "doubao" not in str(schema).lower()
    assert "gpt-image" not in str(schema).lower()


async def test_generate_image_defaults_to_high_and_falls_back_to_normal():
    generated = AsyncMock(
        side_effect=[
            RuntimeError("primary failed"),
            ["data:image/jpeg;base64,abc"],
        ]
    )

    with (
        agent_context(AgentContext()),
        patch("app.agent.image_gen.generate_image", generated),
        patch.object(
            image_tool,
            "upload_and_register",
            new=AsyncMock(return_value=("https://tos/1.png", "1.png")),
        ),
    ):
        result = await image_tool.generate_image.invoke(
            {"query": "a cat", "size": "1920x1080"}
        )

    assert isinstance(result, list)
    assert [call.args[0] for call in generated.call_args_list] == [
        "generate-image-high-model",
        "generate-image-normal-model",
    ]
    assert "modelhub" not in str(result).lower()
    assert "doubao" not in str(result).lower()


async def test_generate_image_normal_uses_only_normal_model():
    generated = AsyncMock(return_value=["data:image/jpeg;base64,abc"])

    with (
        agent_context(AgentContext()),
        patch("app.agent.image_gen.generate_image", generated),
        patch.object(
            image_tool,
            "upload_and_register",
            new=AsyncMock(return_value=("https://tos/1.png", "1.png")),
        ),
    ):
        await image_tool.generate_image.invoke(
            {"query": "a cat", "size": "1920x1080", "quality": "normal"}
        )

    assert [call.args[0] for call in generated.call_args_list] == [
        "generate-image-normal-model",
    ]


async def test_generate_image_high_empty_result_falls_back_to_normal():
    generated = AsyncMock(
        side_effect=[
            [],
            ["data:image/jpeg;base64,abc"],
        ]
    )

    with (
        agent_context(AgentContext()),
        patch("app.agent.image_gen.generate_image", generated),
        patch.object(
            image_tool,
            "upload_and_register",
            new=AsyncMock(return_value=("https://tos/1.png", "1.png")),
        ),
    ):
        await image_tool.generate_image.invoke({"query": "a cat"})

    assert [call.args[0] for call in generated.call_args_list] == [
        "generate-image-high-model",
        "generate-image-normal-model",
    ]


async def test_generate_image_keeps_internal_feature_override():
    generated = AsyncMock(return_value=["data:image/jpeg;base64,abc"])

    with (
        agent_context(AgentContext(features={"image_model": "override-alias"})),
        patch("app.agent.image_gen.generate_image", generated),
        patch.object(
            image_tool,
            "upload_and_register",
            new=AsyncMock(return_value=("https://tos/1.png", "1.png")),
        ),
    ):
        await image_tool.generate_image.invoke(
            {"query": "a cat", "quality": "high"}
        )

    assert [call.args[0] for call in generated.call_args_list] == ["override-alias"]


async def test_generate_image_double_failure_does_not_expose_model_names():
    generated = AsyncMock(
        side_effect=[
            RuntimeError("modelhub/gpt-image-2 failed"),
            RuntimeError("doubao/ep-20251024125110-4xhl4 failed"),
        ]
    )

    with (
        agent_context(AgentContext()),
        patch("app.agent.image_gen.generate_image", generated),
    ):
        result = await image_tool.generate_image.invoke({"query": "a cat"})

    assert isinstance(result, dict)
    visible = str(result).lower()
    assert "modelhub" not in visible
    assert "gpt-image" not in visible
    assert "doubao" not in visible
    assert "ep-20251024125110" not in visible
