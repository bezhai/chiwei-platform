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
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agent.adapters.gemini import _usage_details as gemini_usage_details
from app.agent.adapters.openai import _usage_details as openai_usage_details
from app.agent.trace import collect_usage

pytestmark = pytest.mark.unit

CACHE_KEY = "cache_read_input_tokens"


def _openai_response(prompt: int, completion: int, cached: int) -> SimpleNamespace:
    return SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
            prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
        )
    )


def _gemini_response(prompt: int, completion: int, cached: int) -> SimpleNamespace:
    return SimpleNamespace(
        usage_metadata=SimpleNamespace(
            prompt_token_count=prompt,
            candidates_token_count=completion,
            total_token_count=prompt + completion,
            cached_content_token_count=cached,
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


def test_a_miss_leaves_the_cache_key_off_on_both_sides():
    """没命中就不报这个键：0 会读成"量过、没中"，而 provider 那边其实是没给数。"""
    openai = openai_usage_details(_openai_response(50, 7, 0))
    gemini = gemini_usage_details(_gemini_response(50, 7, 0))

    assert CACHE_KEY not in openai
    assert CACHE_KEY not in gemini


def test_the_round_accumulator_counts_the_key_the_adapters_report():
    """本轮累加器的维度名就是 adapter 报的那个键——两边对不上就永远累加 0。"""
    with collect_usage() as usage:
        assert CACHE_KEY in usage
