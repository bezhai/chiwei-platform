# Gemini Drawing Choice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 GeminiClient 替换失效的 AzureHttpClient，让 `generate_image` 工具可通过灰度走 Gemini 原生画图。

**Architecture:** 新建 `GeminiClient` 继承 `BaseAIClient`，用 `google-genai` SDK 调用 Gemini generateContent API（带 `response_modalities=["IMAGE"]`）。Factory 路由 `google` client_type 到新 client，删除 AzureHttpClient。

**Tech Stack:** Python, google-genai SDK, pytest

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `app/agents/clients/gemini_client.py` | Gemini 画图 client |
| Create | `tests/unit/test_gemini_client.py` | GeminiClient 单元测试 |
| Modify | `app/agents/clients/factory.py` | 路由 google -> GeminiClient |
| Modify | `app/agents/clients/base.py:44` | 移除 AZURE_HTTP 常量 |
| Modify | `app/agents/clients/__init__.py` | 导出更新 |
| Delete | `app/agents/clients/azure_http_client.py` | 废弃 |

---

### Task 1: GeminiClient — 测试 + 实现

**Files:**
- Create: `apps/agent-service/tests/unit/test_gemini_client.py`
- Create: `apps/agent-service/app/agents/clients/gemini_client.py`

- [ ] **Step 1: 写失败测试 — generate_image 正常路径**

```python
"""test_gemini_client.py — GeminiClient 单元测试"""

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


def _fake_model_info(**overrides):
    info = {
        "api_key": "fake-key",
        "base_url": "",
        "model_name": "gemini-2.0-flash-exp",
        "client_type": "google",
        "is_active": True,
    }
    info.update(overrides)
    return info


def _make_response(image_bytes: bytes, mime_type: str = "image/png"):
    """构造 google-genai SDK 风格的 response mock"""
    inline_data = MagicMock()
    inline_data.mime_type = mime_type
    inline_data.data = image_bytes

    part = MagicMock()
    part.inline_data = inline_data
    part.text = None

    candidate = MagicMock()
    candidate.content.parts = [part]

    response = MagicMock()
    response.candidates = [candidate]
    return response


class TestGenerateImage:
    """generate_image 正常路径"""

    async def test_returns_base64_data_uri(self):
        from app.agents.clients.gemini_client import GeminiClient

        client = GeminiClient("test-gemini")

        fake_image = b"\x89PNG_FAKE"
        mock_genai_client = MagicMock()
        mock_genai_client.models.generate_content.return_value = _make_response(fake_image)

        with patch(
            "app.agents.infra.model_builder.ModelBuilder.get_basic_model_params",
            new=AsyncMock(return_value=_fake_model_info()),
        ):
            client._client = mock_genai_client
            client.model_name = "gemini-2.0-flash-exp"

            result = await client.generate_image("a cat", "1K")

        assert len(result) == 1
        expected_b64 = base64.b64encode(fake_image).decode()
        assert result[0] == f"data:image/png;base64,{expected_b64}"

    async def test_with_reference_images(self):
        from app.agents.clients.gemini_client import GeminiClient

        client = GeminiClient("test-gemini")

        fake_image = b"\x89PNG_FAKE"
        mock_genai_client = MagicMock()
        mock_genai_client.models.generate_content.return_value = _make_response(fake_image)

        with patch(
            "app.agents.infra.model_builder.ModelBuilder.get_basic_model_params",
            new=AsyncMock(return_value=_fake_model_info()),
        ):
            client._client = mock_genai_client
            client.model_name = "gemini-2.0-flash-exp"

            result = await client.generate_image(
                "a cat in this style",
                "2K",
                reference_images=["https://example.com/ref.png"],
            )

        assert len(result) == 1
        # 验证调用时包含了 reference image
        call_args = mock_genai_client.models.generate_content.call_args
        contents = call_args.kwargs.get("contents") or call_args[1].get("contents")
        # contents 应该包含参考图片
        assert contents is not None

    async def test_no_candidates_raises(self):
        from app.agents.clients.gemini_client import GeminiClient

        client = GeminiClient("test-gemini")

        mock_genai_client = MagicMock()
        response = MagicMock()
        response.candidates = []
        mock_genai_client.models.generate_content.return_value = response

        client._client = mock_genai_client
        client.model_name = "gemini-2.0-flash-exp"

        with pytest.raises(RuntimeError, match="未返回"):
            await client.generate_image("a cat", "1K")


class TestSizeMapping:
    """尺寸 -> aspectRatio 映射"""

    def test_1k_default(self):
        from app.agents.clients.gemini_client import GeminiClient

        assert GeminiClient._parse_aspect_ratio("1K") == "1:1"

    def test_pixel_format(self):
        from app.agents.clients.gemini_client import GeminiClient

        assert GeminiClient._parse_aspect_ratio("1920x1080") == "16:9"

    def test_square_pixel(self):
        from app.agents.clients.gemini_client import GeminiClient

        assert GeminiClient._parse_aspect_ratio("2048x2048") == "1:1"

    def test_invalid_falls_back(self):
        from app.agents.clients.gemini_client import GeminiClient

        assert GeminiClient._parse_aspect_ratio("invalid") == "1:1"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd apps/agent-service && uv run pytest tests/unit/test_gemini_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.agents.clients.gemini_client'`

- [ ] **Step 3: 实现 GeminiClient**

```python
"""Gemini 原生画图客户端"""

import base64
import logging
from math import gcd

from google import genai
from google.genai import types

from app.agents.clients.base import BaseAIClient

logger = logging.getLogger(__name__)


class GeminiClient(BaseAIClient[genai.Client]):
    """Gemini 原生画图客户端。

    使用 google-genai SDK 调用 generateContent API，
    通过 response_modalities=["IMAGE"] 获取生成图片。
    只实现 generate_image 能力。
    """

    async def _create_client(self, model_info: dict) -> genai.Client:
        return genai.Client(api_key=model_info["api_key"])

    async def disconnect(self) -> None:
        """genai.Client 无需显式关闭。"""
        self._client = None

    @staticmethod
    def _parse_aspect_ratio(size: str) -> str:
        """从 size 参数解析宽高比。

        支持:
        - "1K" / "2K" / "4K": 默认 1:1
        - "WxH": 像素尺寸，自动计算最简比例
        """
        size_str = size.strip().upper()

        if "X" in size_str:
            try:
                w_str, h_str = size_str.split("X", 1)
                w, h = int(w_str), int(h_str)
                if w > 0 and h > 0:
                    g = gcd(w, h)
                    return f"{w // g}:{h // g}"
            except Exception:
                pass

        return "1:1"

    async def generate_image(
        self,
        prompt: str,
        size: str,
        reference_images: list[str] | None = None,
    ) -> list[str]:
        """调用 Gemini generateContent API 生成图片。"""

        client = self._ensure_connected()
        aspect_ratio = self._parse_aspect_ratio(size)

        # 构造 contents
        contents: list[types.Part | str] = []

        if reference_images:
            for url in reference_images:
                contents.append(types.Part.from_uri(file_uri=url, mime_type="image/*"))

        contents.append(prompt)

        response = client.models.generate_content(
            model=self.model_name,
            contents=contents,
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
                image_generation_config=types.ImageGenerationConfig(
                    aspect_ratio=aspect_ratio,
                ),
            ),
        )

        if not response.candidates:
            raise RuntimeError("Gemini 生图接口未返回 candidates")

        images: list[str] = []
        for part in response.candidates[0].content.parts:
            if part.inline_data and part.inline_data.data:
                mime = part.inline_data.mime_type or "image/png"
                b64 = base64.b64encode(part.inline_data.data).decode()
                images.append(f"data:{mime};base64,{b64}")

        if not images:
            raise RuntimeError("Gemini 生图接口未在响应中找到图片数据")

        return images
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd apps/agent-service && uv run pytest tests/unit/test_gemini_client.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/app/agents/clients/gemini_client.py apps/agent-service/tests/unit/test_gemini_client.py
git commit -m "feat(image): add GeminiClient for native Gemini image generation"
```

---

### Task 2: Factory 路由 + 清理 AzureHttpClient

**Files:**
- Modify: `apps/agent-service/app/agents/clients/factory.py`
- Modify: `apps/agent-service/app/agents/clients/base.py:44`
- Modify: `apps/agent-service/app/agents/clients/__init__.py`
- Delete: `apps/agent-service/app/agents/clients/azure_http_client.py`

- [ ] **Step 1: 更新 factory — 添加 google 路由，移除 azure-http**

`factory.py` 改为:

```python
"""客户端工厂"""

from typing import Any

from app.agents.clients.base import BaseAIClient, ClientType


async def create_client(model_id: str) -> BaseAIClient[Any]:
    """根据模型配置创建合适的客户端实例。"""
    from app.agents.clients.ark_client import ArkClient
    from app.agents.clients.gemini_client import GeminiClient
    from app.agents.clients.openai_client import OpenAIClient
    from app.agents.infra.model_builder import ModelBuilder

    model_info = await ModelBuilder._get_model_and_provider_info(model_id)
    if model_info is None or not model_info.get("is_active", True):
        raise ValueError(f"无法获取模型配置或模型未激活: {model_id}")

    client_type = (model_info.get("client_type") or ClientType.OPENAI).lower()

    if client_type == ClientType.OPENAI:
        return OpenAIClient(model_id)
    if client_type == ClientType.ARK:
        return ArkClient(model_id)
    if client_type == ClientType.GOOGLE:
        return GeminiClient(model_id)

    raise ValueError(f"未知的 client_type: {client_type} (model_id={model_id})")
```

- [ ] **Step 2: 从 ClientType 移除 AZURE_HTTP**

`base.py` 中 `ClientType` 类改为:

```python
class ClientType:
    """底层客户端类型枚举。

    主要通过 model_provider.client_type 进行配置：
    - "openai": 标准 OpenAI 兼容（Chat Completions API）
    - "openai-responses": OpenAI Responses API（仅 OpenAI 原生端点）
    - "deepseek": DeepSeek 专用（Completions API + reasoning_content 保留）
    - "ark": 火山引擎 Ark Runtime 客户端
    - "google": Google Generative AI 客户端（Chat + 生图）
    """

    OPENAI = "openai"
    OPENAI_RESPONSES = "openai-responses"
    DEEPSEEK = "deepseek"
    ARK = "ark"
    GOOGLE = "google"
```

- [ ] **Step 3: 更新 `__init__.py` 导出**

```python
"""AI 客户端层

提供统一的 AI 服务客户端抽象，封装具体 API 调用。
"""

from app.agents.clients.ark_client import ArkClient
from app.agents.clients.base import BaseAIClient, ClientType
from app.agents.clients.factory import create_client
from app.agents.clients.gemini_client import GeminiClient
from app.agents.clients.openai_client import OpenAIClient

__all__ = [
    "BaseAIClient",
    "ClientType",
    "OpenAIClient",
    "ArkClient",
    "GeminiClient",
    "create_client",
]
```

- [ ] **Step 4: 删除 azure_http_client.py**

```bash
git rm apps/agent-service/app/agents/clients/azure_http_client.py
```

- [ ] **Step 5: 确认无残留引用**

Run: `cd apps/agent-service && grep -r "azure_http\|AzureHttp\|AZURE_HTTP" app/ tests/ || echo "clean"`
Expected: "clean"

- [ ] **Step 6: 运行全量测试**

Run: `cd apps/agent-service && uv run pytest tests/unit/ -v`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add -A apps/agent-service/app/agents/clients/
git commit -m "refactor(image): replace AzureHttpClient with GeminiClient in factory"
```
