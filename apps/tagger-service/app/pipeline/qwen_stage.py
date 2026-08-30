"""Qwen3-VL 阶段：通过 llama-swap 的 OpenAI 兼容接口，对整批图跑 describe(A/B) + OCR 三次调用。

模型不再由本进程加载：Qwen3-VL 交给 llama-swap 统一调度（GGUF Q8_0 ~11.5 GiB，按需换入换出），
本服务只发 HTTP。因此本阶段既不占显存也没有"加载权重"这一步，load/unload 管的是 HTTP 连接池。

三件实测出来的服务端行为，直接决定了这里的请求怎么发：
1. describe 走 `tools` 参数拿 tool_calls，比让模型吐 JSON 文本再正则解析可靠——llama.cpp 会用语法
   约束把 enum 卡在候选集内（自由输出时实测吐出过 enum 外的 flat_design）。
2. 这是 thinking 模型，不显式关掉思考会把 token 预算全烧在 reasoning_content 上、content 拿回空串，
   所以每个请求都带 chat_template_kwargs.enable_thinking=false。
3. 服务端 -c 16384 -np 2（单 slot 8192 上下文、两个并发 slot），所以图片仍要在客户端降采样，
   默认并发也对齐 slot 数。

失败按"单能力"隔离：某次调用超时 / 500 / 返回体不是 JSON / 形状不认识 / 没有 tool_calls / OCR 被
截断，都只让那一项能力带 error + raw_output 回去，整批其余结果照常产出（与 merge_row 的 errors
语义一致）。发请求和解析响应都在同一个 try 里——解析异常逃出去会让整批 run() 抛异常、任务彻底
失败且不回调任何部分结果，那正是这个设计要避免的。
"""
from __future__ import annotations

import base64
import io
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

import httpx
from PIL import Image, ImageOps

from app.pipeline.ocr_clean import clean_ocr_text
from app.qwen_vl_describe import (
    build_tool,
    build_user_text,
    downscale_dims,
    max_pixels_for_vision_tokens,
    parse_tool_arguments,
)

JPEG_QUALITY = 90


def build_ocr_prompt() -> str:
    """OCR 指令：逐字原样转写图中所有文字、保留换行、无文字则空。

    describe 走 tool calling 约束 key/enum；OCR 是自由文本，另走纯文本 prompt（防退化靠
    max_tokens + assemble_ocr_result 的相邻行去重）。
    """
    return (
        "Transcribe all visible text in this image exactly as it appears, "
        "preserving line breaks. Include text in any language. "
        "Output only the transcribed text with no commentary. "
        "If there is no text, output nothing."
    )


def encode_image_data_url(image: Image.Image, max_pixels: int) -> str:
    """PIL 图 → OpenAI vision 格式要的 `data:image/jpeg;base64,...`。

    先按 EXIF 摆正、压到像素上限再编码：服务端单 slot 上下文有限，大图不压会撑爆 vision token，
    同时 base64 请求体也会大到离谱。
    """
    img = ImageOps.exif_transpose(image).convert("RGB")
    nw, nh = downscale_dims(img.width, img.height, max_pixels)
    if (nw, nh) != (img.width, img.height):
        img = img.resize((nw, nh))
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=JPEG_QUALITY)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _vision_message(data_url: str, text: str) -> list[dict[str, Any]]:
    return [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": text},
            ],
        }
    ]


def build_describe_body(
    model: str, data_url: str, group: str, *, max_new_tokens: int
) -> dict[str, Any]:
    return {
        "model": model,
        "messages": _vision_message(data_url, build_user_text(group)),
        "tools": [build_tool(group)],
        "temperature": 0.0,
        "max_tokens": max_new_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
    }


def build_ocr_body(model: str, data_url: str, *, max_new_tokens: int) -> dict[str, Any]:
    return {
        "model": model,
        "messages": _vision_message(data_url, build_ocr_prompt()),
        "temperature": 0.0,
        "max_tokens": max_new_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
    }


def _first_choice(payload: Any) -> tuple[dict[str, Any] | None, str | None]:
    """从任意形状的响应体里取第一个 choice 的 message + finish_reason，取不到就 (None, None)。

    response.json() 可以回任意合法 JSON（代理页面被当 JSON、错误对象、裸列表、标量），所以每一层
    都得先验形状再取值——这里崩了会炸穿单能力隔离，让整批任务失败且不回调任何部分结果。
    """
    if not isinstance(payload, dict):
        return None, None
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None, None
    choice = choices[0]
    if not isinstance(choice, dict):
        return None, None
    message = choice.get("message")
    if not isinstance(message, dict):
        return None, None
    return message, choice.get("finish_reason")


def _reasoning_of(message: dict[str, Any]) -> str:
    """thinking 模型把思考放在 reasoning_content；正文为空时它就是最有用的诊断材料。"""
    reasoning = message.get("reasoning_content")
    return reasoning if isinstance(reasoning, str) else ""


def parse_describe_response(payload: Any, group: str) -> dict[str, Any]:
    """OpenAI 兼容响应 → describe 字段 dict；拿不到 tool_calls 就是失败，回 error + raw_output。

    只认 tool_calls：`tools` 参数下服务端用语法约束把 enum 卡在候选集内，自由正文没有这层约束
    （实测自由输出吐过 enum 外的 flat_design）。解析正文等于把绕过约束的脏值当成功结果送进
    merge_row，所以这里不做正文回落。
    """
    message, finish_reason = _first_choice(payload)
    if message is None:
        return {"error": "malformed response: no choices", "raw_output": repr(payload)}
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        first = tool_calls[0]
        function = first.get("function") if isinstance(first, dict) else None
        if not isinstance(function, dict):
            return {
                "error": "malformed response: tool call without a function object",
                "raw_output": repr(first),
            }
        arguments = function.get("arguments", "")
        if not isinstance(arguments, str):
            arguments = repr(arguments)
        return parse_tool_arguments(arguments, group)
    return {
        "error": f"no tool calls (finish_reason={finish_reason})",
        "raw_output": _describe_raw_output(message),
    }


def _describe_raw_output(message: dict[str, Any]) -> str:
    """失败诊断素材：优先正文，正文为空时退到 reasoning_content（thinking 烧光预算的典型形态）。"""
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    return _reasoning_of(message)


def assemble_ocr_result(raw_ocr: str) -> dict[str, Any]:
    """OCR 原始输出 → {ocr_text, ocr_len}：先去相邻重复行（防刷屏退化）再 strip。"""
    text = clean_ocr_text(raw_ocr).strip()
    return {"ocr_text": text, "ocr_len": len(text)}


def parse_ocr_response(payload: Any) -> dict[str, Any]:
    """OpenAI 兼容响应 → {ocr_text, ocr_len}。

    只有"正文是字符串 + 正常停止（finish_reason=stop）"才是完整转写：空正文加 stop 就是图里
    没字，合法。其余都是失败——被 length 砍断的是半截转写、正文缺失或不是字符串是响应形状不对、
    其他 finish_reason 是没见过的服务端状态。被截断时把已拿到的部分留在 ocr_text 里便于人工比对，
    但必须同时带 error 让下游知道这条不完整。ocr_text / ocr_len 在所有分支上都在位（merge_row 依赖）。
    """
    message, finish_reason = _first_choice(payload)
    if message is None:
        return {
            "error": "malformed response: no choices",
            "raw_output": repr(payload),
            "ocr_text": "",
            "ocr_len": 0,
        }
    content = message.get("content")
    if not isinstance(content, str):
        return {
            "error": f"malformed response: content is not a string (finish_reason={finish_reason})",
            "raw_output": _reasoning_of(message) or repr(content),
            "ocr_text": "",
            "ocr_len": 0,
        }
    result = assemble_ocr_result(content)
    if finish_reason == "stop":
        return result
    return {
        "error": f"incomplete ocr output (finish_reason={finish_reason})",
        "raw_output": _reasoning_of(message),
        **result,
    }


def request_error_result(exc: Exception) -> dict[str, Any]:
    """调用异常 → 单能力 error dict；HTTP 状态错误顺带把服务端返回体留作 raw_output。"""
    raw = ""
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            raw = response.text
        except Exception:  # pragma: no cover - 极端情况下响应体不可读
            raw = ""
    return {"error": f"{type(exc).__name__}: {exc}", "raw_output": raw}


def _ocr_error_result(exc: Exception) -> dict[str, Any]:
    return {**request_error_result(exc), "ocr_text": "", "ocr_len": 0}


@dataclass(frozen=True)
class QwenVlEndpoint:
    """怎么连上 llama-swap、以什么参数驱动 Qwen3-VL。"""

    base_url: str
    model: str
    api_key: str = ""
    timeout_seconds: float = 180.0
    concurrency: int = 2
    max_new_tokens: int = 512
    max_vision_tokens: int = 4096

    def __post_init__(self) -> None:
        if not self.base_url:
            raise ValueError("qwen endpoint base_url is required")
        if not self.model:
            raise ValueError("qwen endpoint model is required")

    @property
    def chat_completions_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"


class QwenVlHttpStage:
    """对整批 (id, PIL图) 逐图发三次请求（describe A/B + OCR），产出 merge_row 可直接吃的能力字典。

    调度按图并发（默认 2，对齐服务端 slot 数）：每个 worker 编码一张图再串行发它的三次调用，
    在途 base64 图片数因此被并发数钉住，不会像"整批编码再三轮发"那样把整批图片撑在内存里。
    """

    def __init__(self, endpoint: QwenVlEndpoint, *, transport: Any = None) -> None:
        self.endpoint = endpoint
        self.max_pixels = max_pixels_for_vision_tokens(endpoint.max_vision_tokens)
        self._transport = transport
        self._client: httpx.Client | None = None

    @property
    def client(self) -> httpx.Client | None:
        return self._client

    def load(self) -> None:
        """建 HTTP 连接池。这里没有权重加载，不占显存，失败也只是构造客户端失败。"""
        headers = {"Content-Type": "application/json"}
        if self.endpoint.api_key:
            headers["Authorization"] = f"Bearer {self.endpoint.api_key}"
        self._client = httpx.Client(
            timeout=self.endpoint.timeout_seconds,
            headers=headers,
            transport=self._transport,
        )

    def unload(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def run(self, items: list[tuple[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
        if self._client is None:
            raise RuntimeError("QwenVlHttpStage.run called before load()")
        results = self._map(self._process_image, [image for _, image in items])
        return {image_id: result for (image_id, _), result in zip(items, results)}

    def _map(self, fn: Callable[[Any], Any], values: list[Any]) -> list[Any]:
        if self.endpoint.concurrency <= 1 or len(values) <= 1:
            return [fn(value) for value in values]
        # ThreadPoolExecutor.map 按输入顺序回结果，所以并发下 id 与结果的配对仍然成立
        with ThreadPoolExecutor(max_workers=self.endpoint.concurrency) as pool:
            return list(pool.map(fn, values))

    def _process_image(self, image: Any) -> dict[str, dict[str, Any]]:
        try:
            data_url = encode_image_data_url(image, self.max_pixels)
        except Exception as exc:
            # 图本身坏掉：三项能力一起标失败，不发请求
            error = request_error_result(exc)
            return {
                "describe_a": dict(error),
                "describe_b": dict(error),
                "ocr": _ocr_error_result(exc),
            }
        return {
            "describe_a": self._describe(data_url, "a"),
            "describe_b": self._describe(data_url, "b"),
            "ocr": self._ocr(data_url),
        }

    def _describe(self, data_url: str, group: str) -> dict[str, Any]:
        body = build_describe_body(
            self.endpoint.model, data_url, group, max_new_tokens=self.endpoint.max_new_tokens
        )
        try:
            # 解析也在 try 内：单能力隔离要覆盖"响应拿回来了但解析炸了"，否则异常会逃出去让整批
            # run() 抛异常、任务彻底失败且不回调任何部分结果。
            return parse_describe_response(self._post(body), group)
        except Exception as exc:
            return request_error_result(exc)

    def _ocr(self, data_url: str) -> dict[str, Any]:
        body = build_ocr_body(
            self.endpoint.model, data_url, max_new_tokens=self.endpoint.max_new_tokens
        )
        try:
            return parse_ocr_response(self._post(body))
        except Exception as exc:
            return _ocr_error_result(exc)

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        assert self._client is not None  # run() 已挡住未 load 的情况
        response = self._client.post(self.endpoint.chat_completions_url, json=body)
        response.raise_for_status()
        return response.json()
