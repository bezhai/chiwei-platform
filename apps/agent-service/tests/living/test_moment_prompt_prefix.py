"""她这一轮的 SYSTEM 前缀不许跟着钟走。

前缀缓存（Gemini 的隐式缓存、字节网关的 prompt cache）只对"请求开头逐字节相同的那
一段"生效。编译 prompt 时 :meth:`app.agent.core.Agent._prepare` 给**每条** prompt 注
入精确到秒的 ``currTime`` / ``currDate``：SYSTEM 模板一旦引用它们，这一段每次调用都
不同，命中恒为 0。``living_life_moment`` 当前的模板变量是 ``persona_name`` /
``persona_core`` / ``guides_you_can_read`` 三个，都不是时间，所以前缀是稳的。

**这个文件钉得住什么、钉不住什么**：模板正文在 Langfuse 上、不在仓库里，所以"有人在
模板正文里写上 ``{{currTime}}``"这种破坏离线测不出来——它在运行时由
``cache_read_input_tokens`` 暴露（命中数掉到没有，见 ``tests/agent/
test_usage_cache_contract.py``）。离线钉得住的是代码这一侧的两条：

  1. 这一轮交给 prompt 的变量既不叫时间、值也不随钟变（:func:`run_moment` 真跑两轮
     比对）；
  2. 时间只以模板变量的形式进入编译，没有被谁直接拼进 SYSTEM 正文。
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from langfuse.api.resources.prompts import Prompt_Text
from langfuse.model import TextPromptClient

from app.agent.core import Agent
from app.agent.neutral import Message, Role
from app.infra import cst_time
from app.living import moment as moment_mod
from app.living import persona as persona_mod
from app.living.loose_ends import LooseEnd
from app.living.moment import _MOMENT_CFG, LifeMoment, run_moment

LANE = "coe-living"
_CST = dt.timezone(dt.timedelta(hours=8))

# 名字里带这些词的变量，值几乎必然跟着钟走。``currTime`` / ``currDate`` 是
# ``Agent._prepare`` 无条件注入的那两个，首当其冲。
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


class _CapturingLife:
    """替身 life：只把每一轮拿到的 prompt 变量记下来，一句模型都不调。"""

    def __init__(self) -> None:
        self.runs: list[dict] = []

    async def run(self, messages, **kwargs):
        self.runs.append(kwargs)
        return Message(role=Role.ASSISTANT, content="继续")


@pytest.fixture
async def two_moments(living_db, monkeypatch):
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

    runner = _CapturingLife()
    monkeypatch.setattr(persona_mod, "find_persona", fake_find_persona)
    monkeypatch.setattr(moment_mod, "life_moment_minutes", fixed_minutes)
    monkeypatch.setattr(moment_mod, "build_moment_runner", lambda: runner)

    await run_moment(lane=LANE, persona_id="akao", now=_at(14, 0))
    await run_moment(lane=LANE, persona_id="akao", now=_at(14, 10))

    assert len(runner.runs) == 2, f"两轮没都跑起来：{len(runner.runs)} 轮"
    return [run["prompt_vars"] for run in runner.runs]


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
# 时间只从模板变量进 —— 编译这一步不许把钟拼进 SYSTEM 正文
# ---------------------------------------------------------------------------


def _life_moment_prompt(template: str) -> TextPromptClient:
    """一份挂在 living_life_moment 这个 id 下的 text prompt（本地编译，不连网）。"""
    return TextPromptClient(
        Prompt_Text(
            name=_MOMENT_CFG.prompt_id,
            version=1,
            type="text",
            prompt=template,
            config={},
            labels=[],
            tags=[],
        )
    )


async def _system_at(template: str, clock: dt.datetime) -> str:
    """按 ``clock`` 这个时刻编译她这一轮的 prompt，交回 SYSTEM 正文。"""
    prompt = _life_moment_prompt(template)
    with (
        patch("app.agent.core.get_prompt", return_value=prompt),
        patch(
            "app.agent.core.build_model_client",
            new_callable=AsyncMock,
            return_value=AsyncMock(),
        ),
        patch.object(cst_time, "now_cst", lambda: clock),
    ):
        _, messages = await Agent(_MOMENT_CFG)._prepare(
            {
                "persona_name": "赤尾",
                "persona_core": "她拍胶片、写角色分析、逛论坛。",
                "guides_you_can_read": "drawing：人物画图指南",
            }
        )
    return messages[0].content


_STABLE_TEMPLATE = (
    "你是 {{persona_name}}。\n{{persona_core}}\n手边能读的说明：{{guides_you_can_read}}"
)


async def test_the_system_text_does_not_move_with_the_clock():
    """模板不引用时间时，隔一小时编译两次的 SYSTEM 必须逐字节相同。

    钉的是编译这一步：``currTime`` / ``currDate`` 只作为模板变量传进去，谁也不许把
    "现在几点"直接拼进 SYSTEM 正文——那样模板写得再干净前缀也是每次都变。
    """
    early = await _system_at(_STABLE_TEMPLATE, _at(14, 0))
    later = await _system_at(_STABLE_TEMPLATE, _at(15, 30))

    assert early == later
    assert "14:00" not in early and "15:30" not in later


async def test_a_template_that_reads_the_clock_moves_every_call():
    """反过来验一遍上面那条不是空的：模板一旦用上 ``currTime``，两次就不一样了。

    没有这条，上面那个"两次相同"在注入被整个删掉时也照样绿——它证明的就只是
    "SYSTEM 里没有时间"，而不是"时间确实在手边、只是模板没用"。
    """
    template = _STABLE_TEMPLATE + "\n现在是 {{currDate}} {{currTime}}"

    early = await _system_at(template, _at(14, 0))
    later = await _system_at(template, _at(15, 30))

    assert early != later
    assert "14:00:00" in early and "15:30:00" in later
