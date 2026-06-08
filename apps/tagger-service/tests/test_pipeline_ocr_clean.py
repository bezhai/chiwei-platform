from __future__ import annotations

from app.pipeline.ocr_clean import clean_ocr_text


def test_clean_removes_consecutive_duplicate_lines() -> None:
    # OCR 退化最常见形态：同一行反复刷屏（vLLM 无 no_repeat_ngram_size、靠后处理兜底）
    raw = "月刊「浅い海」\n月刊「浅い海」\n月刊「浅い海」\n结尾不同"
    out = clean_ocr_text(raw)
    assert out.count("月刊「浅い海」") == 1
    assert "结尾不同" in out


def test_clean_collapses_blank_runs() -> None:
    # 连续空行压成一个（空行也是相邻重复）
    assert clean_ocr_text("a\n\n\n\nb") == "a\n\nb"


def test_clean_keeps_normal_text() -> None:
    t = "这是一段正常文字\n第二行内容不同\n第三行也不同"
    assert clean_ocr_text(t) == t


def test_clean_empty() -> None:
    assert clean_ocr_text("") == ""
