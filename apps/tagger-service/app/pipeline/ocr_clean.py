"""OCR 输出防退化后处理。

vLLM 不支持 no_repeat_ngram_size，OCR 退化最常见形态是"同一行反复刷屏"——这里去掉相邻
重复行（连续空行也是相邻重复、顺带压成一个）。段落级循环（行1/行2 交替）不在 MVP 处理，
按 OCR"尽量识别不追精度"调性接受。
"""
from __future__ import annotations


def clean_ocr_text(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    for line in lines:
        if out and line == out[-1]:
            continue
        out.append(line)
    return "\n".join(out)
