"""两个 adapter 的缓存命中上报必须是同一个口径。

缓存命中的 token 数从 provider 响应里取出来后，要经过三段才变成可查的数字：
adapter 的 ``usage_details`` → :func:`app.agent.trace.collect_usage` 的本轮累加
→ ``ThinkingTokensSpent.cached_tokens`` 落 PG（langfuse 那一路同时拿同一个 dict）。
中间两段按**字段名**取数，所以两个 adapter 各自起一个名字就等于其中一条线永远是 0，
而且不报错。这个文件钉住那个名字，以及"缓存命中的 token 已经算在 input 里"这个口径。

两个 provider 原始字段不同名、藏的层数也不同：

  * OpenAI 家族（含字节 GPT 网关）：``usage.prompt_tokens_details.cached_tokens``
  * Gemini 原生：``usage_metadata.cached_content_token_count``

两边都是"命中的这部分已经计在 prompt token 里"，所以 ``cache_read_input_tokens``
永远是 ``input`` 的子集，不能再加进 input 或 total。

**报了 0 和没报是两件事。** provider 给了这个字段、值是 0 =「量过、这次没中」；字段
根本不在 = 这次调用没有缓存数据可谈。压成同一个形状（0 就把键丢掉）之后，命中率的
分母里有多少次调用根本没测过就看不出来了 —— 先前误判"命中恒为 0"正是这么来的。
所以：provider 报了就带着（哪怕是 0），没报才不带。

思考 token 同一条规则：gemini 报 ``thoughts_token_count``，openai 家族报
``completion_tokens_details.reasoning_tokens``，两边都落到 ``thinking_tokens``。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agent.adapters.gemini import _usage_details as gemini_usage_details
from app.agent.adapters.openai import _usage_details as openai_usage_details
from app.agent.trace import collect_usage

pytestmark = pytest.mark.unit

CACHE_KEY = "cache_read_input_tokens"
THINKING_KEY = "thinking_tokens"


def _total(prompt: int | None, completion: int | None) -> int | None:
    if prompt is None and completion is None:
        return None
    return (prompt or 0) + (completion or 0)


def _openai_response(
    prompt: int | None,
    completion: int | None,
    cached: int | None,
    thinking: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=_total(prompt, completion),
            prompt_tokens_details=(
                None if cached is None else SimpleNamespace(cached_tokens=cached)
            ),
            completion_tokens_details=(
                None
                if thinking is None
                else SimpleNamespace(reasoning_tokens=thinking)
            ),
        )
    )


def _gemini_response(
    prompt: int | None,
    completion: int | None,
    cached: int | None,
    thinking: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        usage_metadata=SimpleNamespace(
            prompt_token_count=prompt,
            candidates_token_count=completion,
            total_token_count=_total(prompt, completion),
            cached_content_token_count=cached,
            thoughts_token_count=thinking,
        )
    )


def test_both_adapters_name_a_cache_hit_the_same_thing():
    """同一个命中数，两个 adapter 报出来必须是同一个键。"""
    openai = openai_usage_details(_openai_response(62000, 18, 60000))
    gemini = gemini_usage_details(_gemini_response(62000, 18, 60000))

    assert openai[CACHE_KEY] == 60000
    assert gemini[CACHE_KEY] == 60000
    assert openai.keys() == gemini.keys()


def test_a_cache_hit_is_counted_inside_input_not_on_top_of_it():
    """命中的 token 已经算在 input 里，两边同口径——不能再往 input/total 上加。"""
    openai = openai_usage_details(_openai_response(62000, 18, 60000))
    gemini = gemini_usage_details(_gemini_response(62000, 18, 60000))

    for details in (openai, gemini):
        assert details["input"] == 62000
        assert details["total"] == 62018
        assert details[CACHE_KEY] <= details["input"]


def test_a_reported_miss_is_a_zero_not_a_missing_key():
    """provider 说了"这次 0"就记 0 —— 丢掉它等于把"量过没中"说成"没量过"。"""
    openai = openai_usage_details(_openai_response(50, 7, 0))
    gemini = gemini_usage_details(_gemini_response(50, 7, 0))

    assert openai[CACHE_KEY] == 0
    assert gemini[CACHE_KEY] == 0


def test_a_provider_that_said_nothing_leaves_the_cache_key_off():
    """字段根本不在：这次调用没有缓存数据可谈，键就不能出现。"""
    openai = openai_usage_details(_openai_response(50, 7, None))
    gemini = gemini_usage_details(_gemini_response(50, 7, None))

    assert CACHE_KEY not in openai
    assert CACHE_KEY not in gemini


def test_both_adapters_name_the_thinking_tokens_the_same_thing():
    openai = openai_usage_details(_openai_response(50, 7, None, thinking=120))
    gemini = gemini_usage_details(_gemini_response(50, 7, None, thinking=120))

    assert openai[THINKING_KEY] == 120
    assert gemini[THINKING_KEY] == 120


def test_an_input_or_output_the_provider_never_reported_is_absent_not_a_zero():
    """四个维度同一条规则：没报就是没报，不能写成 0。

    写成 0 之后，指标上读到的是「量过、这次 0」，而真相是这次调用根本没有这项数据 ——
    正是缓存那一维踩过的坑，input / output 没有理由例外。
    """
    openai = openai_usage_details(_openai_response(None, None, None))
    gemini = gemini_usage_details(_gemini_response(None, None, None))

    for details in (openai, gemini):
        assert "input" not in details
        assert "output" not in details


def test_a_reported_zero_input_or_output_is_still_a_zero():
    openai = openai_usage_details(_openai_response(0, 0, None))
    gemini = gemini_usage_details(_gemini_response(0, 0, None))

    for details in (openai, gemini):
        assert details["input"] == 0
        assert details["output"] == 0


def test_thinking_tokens_are_absent_when_the_provider_never_reported_them():
    openai = openai_usage_details(_openai_response(50, 7, None))
    gemini = gemini_usage_details(_gemini_response(50, 7, None))

    assert THINKING_KEY not in openai
    assert THINKING_KEY not in gemini


def test_the_round_accumulator_counts_the_key_the_adapters_report():
    """本轮累加器的维度名就是 adapter 报的那个键——两边对不上就永远累加 0。"""
    with collect_usage() as usage:
        assert CACHE_KEY in usage
