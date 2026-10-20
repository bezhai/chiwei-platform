"""她这一轮的 SYSTEM 前缀不许跟着钟走。

前缀缓存（Gemini 的隐式缓存、字节网关的 prompt cache）只对"请求开头逐字节相同的那
一段"生效。SYSTEM 模板一旦引用一个跟着钟走的变量，这一段每次调用都不同，命中恒为 0。
``living_life_moment`` 的模板变量是 ``persona_name`` / ``persona_core`` /
``guides_you_can_read`` 三个，都不是时间，所以前缀是稳的；编译时也不再注入任何时间变量
（原来每条 prompt 都会收到精确到秒的 ``currTime`` / ``currDate``）。

**这个文件钉得住什么、钉不住什么**：模板正文在 Langfuse 上、不在仓库里，所以"有人在
模板正文里写上 ``{{currTime}}``"这种破坏离线测不出来——它在运行时由
``cache_read_input_tokens`` 暴露（命中数掉到没有，见 ``tests/agent/
test_usage_cache_contract.py``）。离线钉得住的是代码这一侧的两条：

  1. 这一轮交给 prompt 的变量既不叫时间、值也不随钟变（她的常规节奏真跑两轮
     比对）；
  2. 编译这一步不把钟拼进 SYSTEM 正文；模板要是声明了一个时间变量，这一轮不会悄悄给它一个
     值，而是按配置错误失败（:class:`app.agent.prompts.PromptConfigError`）。
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from langfuse.api.resources.prompts import Prompt_Text
from langfuse.model import TextPromptClient

from app.agent.prompts import PromptConfigError, PromptSet
from app.infra import cst_time
from app.infra.propagation import Context
from app.living import moment as moment_mod
from app.living.loose_ends.threads import LooseEnd
from app.living.moment import (
    LIFE_MOMENT_AGENT,
    LIFE_MOMENT_PROMPT_ID,
    LIFE_MOMENT_SEGMENTS,
    LifeMoment,
)
from app.living.persona import chain as persona_mod
from app.living.persona import identity_segment
from app.living.persona.chain import PersonaChain
from tests.living.conftest import Calls, regular_round

LANE = "coe-living"
_CST = dt.timezone(dt.timedelta(hours=8))

# 名字里带这些词的变量，值几乎必然跟着钟走。``currTime`` / ``currDate`` 是原来每条 prompt
# 都会被注入的那两个，首当其冲。
_CLOCK_WORDS = frozenset(
    {
        "currtime",
        "currdate",
        "time",
        "date",
        "datetime",
        "now",
        "today",
        "clock",
        "hour",
        "minute",
        "second",
        "timestamp",
        "at",
    }
)


def _clock_shaped(name: str) -> bool:
    """变量名按 ``_`` 和大小写拆开后，含不含时间词。"""
    tokens = {name.lower()}
    tokens.update(part for part in name.lower().split("_") if part)
    return bool(tokens & _CLOCK_WORDS)


@pytest.fixture
async def two_moments(living_db, monkeypatch, models, resident):
    """真跑她相隔十分钟的两轮，交回这两轮各自喂给 prompt 的变量。

    这一轮该不该跑、变量怎么组装走的都是真代码；只有"调模型"那一步是替身，
    ``bot_persona`` 和 Dynamic Config 这两处外部读取按住不放。
    """
    from tests.runtime.conftest import migrate

    for cls in (LooseEnd, LifeMoment):
        await migrate(cls, living_db)

    async def fake_find_persona(persona_id: str):
        return SimpleNamespace(display_name="赤尾", persona_core="她拍胶片、逛论坛。")

    async def fixed_minutes() -> int:
        return 10

    runner = Calls()
    monkeypatch.setattr(persona_mod, "find_persona", fake_find_persona)
    monkeypatch.setattr(moment_mod, "life_moment_minutes", fixed_minutes)
    models[LIFE_MOMENT_PROMPT_ID] = runner.client

    await regular_round(resident, "akao", _at(14, 0))
    await regular_round(resident, "akao", _at(14, 10))

    assert len(runner.runs) == 2, f"两轮没都跑起来：{len(runner.runs)} 轮"
    return [run["values"] for _messages, run in runner.runs]


def _at(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 7, 25, hour, minute, tzinfo=_CST)


@pytest.mark.integration
async def test_two_moments_hand_the_prompt_the_same_variables(two_moments):
    """相隔十分钟的两轮，喂给 prompt 的变量必须一字不差。

    有一个变量的值跟着钟走（比如把"现在几点"塞进 persona 正文），SYSTEM 就每轮都不
    一样，前缀缓存归零——而且这件事不会报错，只会体现在账单上。
    """
    first, second = two_moments

    assert first == second, "两轮之间有变量变了，SYSTEM 前缀跟着钟走了"


@pytest.mark.integration
async def test_the_life_moment_prompt_variables_are_not_named_after_the_clock(
    two_moments,
):
    """这一轮交给 prompt 的变量名里不许出现时间类的词。

    值相等只说明这两轮之间它没变（persona 正文这种慢变量本来也不会十分钟一变），名
    字是另一道：叫 ``now`` / ``currTime`` 的变量迟早会按它名字的意思被填上。
    """
    supplied = set(two_moments[0])

    clock_named = {name for name in supplied if _clock_shaped(name)}
    assert not clock_named, f"这一轮的 prompt 变量里混进了时间：{clock_named}"


# ---------------------------------------------------------------------------
# 编译这一步不许把钟拼进 SYSTEM 正文
# ---------------------------------------------------------------------------


def _life_moment_prompt(prompt_id: str, template: str) -> TextPromptClient:
    """她这一轮的一段 text prompt（本地编译，不连网）。"""
    return TextPromptClient(
        Prompt_Text(
            name=prompt_id,
            version=1,
            type="text",
            prompt=template,
            config={},
            labels=[],
            tags=[],
        )
    )


async def _system_at(templates: dict[str, str], clock: dt.datetime) -> str:
    """按 ``clock`` 这个时刻开始的一轮取齐、编译她这一轮的 prompt（``templates`` 是每一段的模板，按
    段名），交回 SYSTEM 正文。变量是这一轮算出来的（「她是谁」那一段是 persona 插件的
    :func:`app.living.persona.identity_segment`，「手边能读的说明」那一段见
    :data:`app.living.moment.LIFE_MOMENT_SEGMENTS`），「她是谁」和手边的说明按住不放。"""
    from app.agent.round import Round

    langfuse = SimpleNamespace(
        get_prompt=lambda prompt_id, **_k: _life_moment_prompt(
            prompt_id, templates[prompt_id.rsplit("/", 1)[1]]
        )
    )

    async def who_she_is(*, lane, persona_id):
        return {
            "persona_name": "赤尾",
            "persona_core": "她拍胶片、写角色分析、逛论坛。",
        }

    chain = PersonaChain()

    round = Round(
        app="agent-service",
        lane="coe-living",
        participant="akao",
        profile="resident",
        round_id=clock.isoformat(timespec="minutes"),
        now=clock,
        reason="常规节奏",
        due=None,
    )
    with (
        patch("app.agent.prompts._get_client", return_value=langfuse),
        patch(
            "app.agent.prompts.current_context",
            return_value=Context(trace_id=None, lane=None),
        ),
        patch.object(cst_time, "now_cst", lambda: clock),
        patch.object(chain, "prompt_vars", who_she_is),
        patch.object(
            moment_mod, "guides_she_can_read", lambda: "drawing：人物画图指南"
        ),
    ):
        prompts = await PromptSet.resolve(
            {LIFE_MOMENT_PROMPT_ID: LIFE_MOMENT_AGENT},
            {
                segment.name: segment
                for segment in (identity_segment(chain), *LIFE_MOMENT_SEGMENTS)
            },
            round,
        )
        [system] = prompts.messages(LIFE_MOMENT_PROMPT_ID)
    return system.content


_STABLE_TEMPLATES = {
    "identity": "你是 {{persona_name}}。\n{{persona_core}}",
    "this-moment": "<this-moment>\n此刻。\n</this-moment>",
    "rules": "<rules>\n规则。\n</rules>",
    "voice": "<voice>\n说话。\n</voice>",
    "guides": "手边能读的说明：{{guides_you_can_read}}",
}


async def test_the_system_text_does_not_move_with_the_clock():
    """模板不引用时间时，隔一小时编译两次的 SYSTEM 必须逐字节相同。

    钉的是编译这一步：谁也不许把"现在几点"直接拼进 SYSTEM 正文——那样模板写得再干净前缀也是
    每次都变。
    """
    early = await _system_at(_STABLE_TEMPLATES, _at(14, 0))
    later = await _system_at(_STABLE_TEMPLATES, _at(15, 30))

    assert early == later
    assert "14:00" not in early and "15:30" not in later


async def test_a_template_that_reads_the_clock_is_a_configuration_error():
    """反过来：模板一旦用上 ``currTime`` / ``currDate``，这一轮不会悄悄给它们一个值（那样前缀
    就跟着钟走了），也不会把字面的 ``{{currTime}}`` 发给模型，而是在取齐时按配置错误失败。"""
    templates = {
        **_STABLE_TEMPLATES,
        "this-moment": "<this-moment>\n现在是 {{currDate}} {{currTime}}\n</this-moment>",
    }

    with pytest.raises(PromptConfigError) as raised:
        await _system_at(templates, _at(14, 0))

    assert raised.value.unexpected == frozenset({"currDate", "currTime"})
    assert raised.value.missing == frozenset()
