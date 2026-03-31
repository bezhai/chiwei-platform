# Gemini Drawing Choice

## 背景

当前 `generate_image` 工具通过 `AzureHttpClient` 调用画图接口，该接口已失效。需替换为 Gemini 原生画图能力（`google-genai` SDK），并通过灰度机制切入。

## 改动范围

### 1. 新建 `GeminiClient`

**文件**: `apps/agent-service/app/agents/clients/gemini_client.py`

- 继承 `BaseAIClient[genai.Client]`
- `_create_client`: 用 `model_info["api_key"]` 和 `model_info["base_url"]`（如有）初始化 `genai.Client`
- `generate_image`: 调用 `client.models.generate_content()`，带 `response_modalities=["IMAGE"]` 和 `image_generation_config`
- 尺寸映射复用 AzureHttpClient 的逻辑（`1K/2K/4K` + `WxH` 像素解析 -> aspectRatio）
- 返回 `data:{mime_type};base64,{data}` 格式，与现有上传管线兼容
- `disconnect`: `genai.Client` 无需显式关闭，override 为 no-op

### 2. 更新 Factory

**文件**: `apps/agent-service/app/agents/clients/factory.py`

- 添加 `ClientType.GOOGLE` -> `GeminiClient` 的路由
- 移除 `ClientType.AZURE_HTTP` -> `AzureHttpClient` 的路由

### 3. 清理 AzureHttpClient

- 删除 `apps/agent-service/app/agents/clients/azure_http_client.py`
- 从 `ClientType` 中移除 `AZURE_HTTP = "azure-http"` 常量

### 4. 灰度切入

数据库操作（不涉及代码改动）：
1. 在 `model_provider` 表新增一条 `client_type="google"` 的 provider
2. 在 `model_mapping` 表新增 alias 指向该 provider
3. 通过 `gray_config.image_model` 将指定会话路由到 Gemini 画图

## 不改动

- `generate_image` 工具（`tools/image/generate.py`）— 灰度机制已有
- `BaseAIClient` 接口 — 不变
- 其他 client（OpenAI / Ark）— 不动
- 依赖 — `google-genai>=1.0.0` 已在 pyproject.toml 中

## Gemini Image Generation API 要点

```python
from google import genai
from google.genai import types

client = genai.Client(api_key=api_key)

response = client.models.generate_content(
    model=model_name,  # e.g. "gemini-2.0-flash-exp"
    contents=contents,
    config=types.GenerateContentConfig(
        response_modalities=["IMAGE"],
        image_generation_config=types.ImageGenerationConfig(
            aspect_ratio="1:1",  # 从 size 参数映射
        ),
    ),
)

# 解析返回
for part in response.candidates[0].content.parts:
    if part.inline_data:
        mime_type = part.inline_data.mime_type  # "image/png"
        data = part.inline_data.data  # bytes
```
