"""Qwen3-VL GPU 阶段：一个 vLLM 实例对整批图跑 describe(A/B)+ OCR 三轮，产出每 id 的合并能力。

复用 qwen_vl_describe 的纯函数（parse_tool_call 等）和 ocr_clean 的退化清洗；本模块只新增 OCR
prompt、单图结果组装、整批组装这几个纯函数。持有 vLLM 实例、load→三轮 generate→unload 的薄壳
依赖 GPU，端到端在 .206 验（spike 已确认进程内 del llm + destroy_*_parallel 卸载干净、可方案 A）。
"""
from __future__ import annotations

import gc
import os
from typing import Any

from PIL import Image, ImageOps

from app.pipeline.ocr_clean import clean_ocr_text
from app.qwen_vl_describe import (
    build_image_size_constraint,
    build_tool,
    build_user_text,
    downscale_dims,
    merge_alloc_conf,
    parse_tool_call,
    resolve_vision_token_cap,
)


def build_ocr_prompt() -> str:
    """OCR 指令：逐字原样转写图中所有文字、保留换行、无文字则空。

    describe 走 tool calling 软约束 key/enum；OCR 是自由文本，另走纯文本 prompt（防退化靠
    repetition_penalty + max_tokens + assemble_ocr_result 的相邻去重三层，见 spec decision 5）。
    """
    return (
        "Transcribe all visible text in this image exactly as it appears, "
        "preserving line breaks. Include text in any language. "
        "Output only the transcribed text with no commentary. "
        "If there is no text, output nothing."
    )


def assemble_describe_result(raw_a: str, raw_b: str) -> dict[str, dict[str, Any]]:
    """两轮 describe 原始输出 → {describe_a, describe_b}，各走 parse_tool_call（解析失败保留 error+raw）。"""
    return {
        "describe_a": parse_tool_call(raw_a, "a"),
        "describe_b": parse_tool_call(raw_b, "b"),
    }


def assemble_ocr_result(raw_ocr: str) -> dict[str, Any]:
    """OCR 原始输出 → {ocr_text, ocr_len}：先去相邻重复行（防 vLLM 退化刷屏）再 strip。"""
    text = clean_ocr_text(raw_ocr).strip()
    return {"ocr_text": text, "ocr_len": len(text)}


def assemble_stage_results(
    ids: list[str],
    raws_a: list[str],
    raws_b: list[str],
    raws_ocr: list[str],
) -> dict[str, dict[str, dict[str, Any]]]:
    """整批三轮 generate 的原始输出按 id 组装成 {id: {describe_a, describe_b, ocr}}，直接喂 merge_row。"""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for i, image_id in enumerate(ids):
        out[image_id] = {
            **assemble_describe_result(raws_a[i], raws_b[i]),
            "ocr": assemble_ocr_result(raws_ocr[i]),
        }
    return out


class QwenVllmStage:
    """持有一个 Qwen3-VL vLLM 实例，对整批图跑 describe(A/B)+ OCR 三轮 generate（共享实例、不额外占显存）。

    load → run(整批) → unload 三段。GPU 重依赖（vllm/torch/transformers）在方法内 import，本机
    import 本模块不触发 GPU。卸载序列经 spike 验证（del llm + destroy_*_parallel + empty_cache →
    显存回 0、EngineCore 子进程干净退出、同进程可接下一阶段），故走方案 A 进程内 load-unload。
    参数默认对齐 describe 全量（关 mm/prefix 缓存防 RSS 泄漏、size 约束防大图崩）。
    """

    def __init__(
        self,
        model_path: str,
        *,
        max_model_len: int = 16384,
        gpu_mem_util: float = 0.88,
        max_num_seqs: int = 2,
        max_vision_tokens: int = 8192,
        text_reserve: int = 2048,
        max_new_tokens: int = 512,
    ) -> None:
        self.model_path = model_path
        self.max_model_len = max_model_len
        self.gpu_mem_util = gpu_mem_util
        self.max_num_seqs = max_num_seqs
        self.max_vision_tokens = max_vision_tokens
        self.text_reserve = text_reserve
        self.max_new_tokens = max_new_tokens
        self.llm: Any = None
        self.processor: Any = None
        self.max_pixels: int = 0
        self._describe_sp: Any = None
        self._ocr_sp: Any = None

    def load(self) -> None:
        os.environ["PYTORCH_CUDA_ALLOC_CONF"] = merge_alloc_conf(
            os.environ.get("PYTORCH_CUDA_ALLOC_CONF", "")
        )
        from transformers import AutoProcessor
        from vllm import LLM, SamplingParams

        self.processor = AutoProcessor.from_pretrained(self.model_path)
        cap = resolve_vision_token_cap(self.max_vision_tokens, self.max_model_len, self.text_reserve)
        size = build_image_size_constraint(cap)
        self.max_pixels = size["longest_edge"]
        self.llm = LLM(
            model=self.model_path,
            max_model_len=self.max_model_len,
            limit_mm_per_prompt={"image": 1},
            gpu_memory_utilization=self.gpu_mem_util,
            max_num_seqs=self.max_num_seqs,
            enforce_eager=True,
            mm_processor_cache_gb=0,
            enable_prefix_caching=False,
            mm_processor_kwargs={"size": size},
        )
        # describe 和 OCR 都只保留 repetition_penalty 防自由文本退化、贪心解码；不带 no_repeat_ngram_size。
        self._describe_sp = SamplingParams(
            temperature=0.0, max_tokens=self.max_new_tokens, repetition_penalty=1.05
        )
        self._ocr_sp = SamplingParams(
            temperature=0.0, max_tokens=self.max_new_tokens, repetition_penalty=1.05
        )

    def _prep_image(self, image: Image.Image) -> Image.Image:
        img = ImageOps.exif_transpose(image).convert("RGB")
        nw, nh = downscale_dims(img.width, img.height, self.max_pixels)
        if (nw, nh) != (img.width, img.height):
            img = img.resize((nw, nh))
        return img

    def _describe_inputs(self, images: list[Image.Image], group: str) -> list[dict[str, Any]]:
        tool = build_tool(group)
        question = build_user_text(group)
        inputs: list[dict[str, Any]] = []
        for image in images:
            img = self._prep_image(image)
            messages = [{"role": "user", "content": [
                {"type": "image"},
                {"type": "text", "text": question},
            ]}]
            text = self.processor.apply_chat_template(
                messages, tools=[tool], tokenize=False, add_generation_prompt=True
            )
            inputs.append({"prompt": text, "multi_modal_data": {"image": img}})
        return inputs

    def _ocr_inputs(self, images: list[Image.Image]) -> list[dict[str, Any]]:
        question = build_ocr_prompt()
        inputs: list[dict[str, Any]] = []
        for image in images:
            img = self._prep_image(image)
            messages = [{"role": "user", "content": [
                {"type": "image"},
                {"type": "text", "text": question},
            ]}]
            text = self.processor.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            )
            inputs.append({"prompt": text, "multi_modal_data": {"image": img}})
        return inputs

    def run(self, items: list[tuple[str, Image.Image]]) -> dict[str, dict[str, dict[str, Any]]]:
        """对整批 (id, PIL图) 跑三轮 generate，返回 {id: {describe_a, describe_b, ocr}} 供 merge_row。

        整批一次性构造输入并 generate（无内部 chunk 分块，对齐 spec「阶段内对整批循环推理」）：
        调用方须控制单批大小——整批 PIL + 整批 prompt 驻留 host RAM，批过大会重蹈 describe 全量的
        RAM 撑爆（见 notes.md，全量靠 chunk 50 才稳）。OOM/分块属 MVP 后系统级鲁棒性、本轮不做。
        """
        ids = [image_id for image_id, _ in items]
        images = [image for _, image in items]
        raws_a = [o.outputs[0].text for o in self.llm.generate(self._describe_inputs(images, "a"), self._describe_sp)]
        raws_b = [o.outputs[0].text for o in self.llm.generate(self._describe_inputs(images, "b"), self._describe_sp)]
        raws_ocr = [o.outputs[0].text for o in self.llm.generate(self._ocr_inputs(images), self._ocr_sp)]
        return assemble_stage_results(ids, raws_a, raws_b, raws_ocr)

    def unload(self) -> None:
        """spike 验证过的卸载序列：显存回 0、EngineCore 子进程干净退出、同进程可接下一阶段。"""
        import torch
        from vllm.distributed.parallel_state import (
            destroy_distributed_environment,
            destroy_model_parallel,
        )

        self.llm = None
        self.processor = None
        gc.collect()
        destroy_model_parallel()
        destroy_distributed_environment()
        gc.collect()
        torch.cuda.empty_cache()
