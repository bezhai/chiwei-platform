"""在场匹配（presence_match）契约 — Task 3：事件投递 = 客观作用域 + 在场匹配.

新范式把「这条客观动静该投给谁」从 world 主观挑收件人改成**客观作用域 + 在场
匹配**：world 产事件时标这事的客观作用域（发生在哪、广播性的还是指向具体某人），
再由一道**纯模型判断**对一下每个角色此刻客观在哪（从 life 的 ``current_state``
自然语言读），谁在场谁就收到。

赤尾宪法红线（这些测试钉死）：

  * 在场匹配**必须是模型判断**——这里 mock 掉那次 LLM 调用，断言它真的被调到了
    （不是代码里写死的距离阈值 / 位置枚举 / if 分支）。
  * 喂给匹配模型的输入**只含**「事件作用域 + 各角色此刻客观位置」，**绝不含**
    「谁该不该被叫醒 / 状态多久没动 / next_wake_at」这类唤醒语义——在场匹配只看
    「事件发生在哪 + 谁在那」，不看「谁该醒」，所以不自锁。
  * 模糊在场（下雨在室内还是室外、看没看见窗外）留给模型判断，代码不加规则消除。
"""

from __future__ import annotations

import pytest

import app.world.presence_match as pm
from app.world.presence_match import (
    PresenceVerdict,
    build_presence_messages,
    match_present_personas,
)


@pytest.fixture
def _mock_extract(monkeypatch):
    """mock 在场匹配那次 LLM（Agent.extract）：记录喂进去的 messages，回放脚本判定。

    返回一个 ``set_present(persona_ids)`` 让每个用例定本次匹配的判定结果，外加
    ``calls`` 收集每次 extract 的 (response_model, messages) 供断言「输入里有什么、
    没有什么」。在场匹配是模型判断——这里只换掉模型本身，匹配逻辑全在被 mock 的
    那次调用里（代码侧不许有任何确定性匹配规则）。
    """
    state = {"present": []}
    calls: list[dict] = []

    async def fake_extract(self, response_model, messages, **kwargs):
        calls.append({"response_model": response_model, "messages": messages})
        return PresenceVerdict(present=list(state["present"]))

    monkeypatch.setattr(pm.Agent, "extract", fake_extract, raising=True)

    def set_present(persona_ids):
        state["present"] = list(persona_ids)

    set_present.calls = calls  # type: ignore[attr-defined]
    return set_present


def _messages_text(calls) -> str:
    """把本次 extract 喂进去的所有 message 文本拼起来，供「含 / 不含」断言。"""
    assert len(calls) == 1, "在场匹配应当只调一次 LLM"
    msgs = calls[0]["messages"]
    return "\n".join(m.content for m in msgs)


@pytest.mark.asyncio
async def test_broadcast_scope_only_present_personas_receive(_mock_extract):
    """广播作用域「下课铃响，全校广播」→ 只有在学校的姐妹被判在场、收到。

    千凪在公司、绫奈在学校、赤尾在家——下课铃是全校广播，只有在学校的绫奈够得着。
    在场判定是模型给的（这里 mock 成只返绫奈），代码不靠任何位置枚举 / 关键词匹配。
    """
    _mock_extract(["ayana"])

    present = await match_present_personas(
        scope="下课铃响，整座学校都听得到——这是全校范围的广播动静。",
        persona_locations={
            "chinagi": "在公司工位上对着电脑改方案",
            "ayana": "在学校教室里上课",
            "akao": "在家厨房备午饭",
        },
    )

    assert present == ["ayana"], "只有在学校的绫奈够得着全校广播的下课铃"


@pytest.mark.asyncio
async def test_directed_scope_only_targeted_present_person_receives(_mock_extract):
    """指向性作用域「妈妈喊绫奈吃饭」→ 只有在家的绫奈收到。

    这条动静客观上发生在家、指向绫奈；在学校的、在公司的够不着。模型判定（mock
    成只返绫奈）——指向性同样是「事件发生在哪 + 谁在那」的客观判断，不是 world
    主观挑收件人。
    """
    _mock_extract(["ayana"])

    present = await match_present_personas(
        scope="家里厨房，妈妈朝餐桌方向喊绫奈来吃饭——声音在屋里传得到。",
        persona_locations={
            "chinagi": "在公司加班",
            "ayana": "在家客厅写作业",
            "akao": "在学校自习室",
        },
    )

    assert present == ["ayana"], "只有在家的绫奈听得到妈妈在屋里喊她吃饭"


@pytest.mark.asyncio
async def test_match_input_carries_scope_and_each_location(_mock_extract):
    """喂给匹配模型的输入必须含「事件作用域 + 每个角色此刻客观位置」。

    在场匹配只看这两样就能判——作用域（事件发生在哪、广播还是指向谁）+ 各角色在哪。
    缺任一样模型就无从判在场。断言原文进了 prompt（不是被代码预消化成结构化标签）。
    """
    _mock_extract(["ayana", "akao"])

    await match_present_personas(
        scope="客厅传来开关门的声音——在屋里的人听得到。",
        persona_locations={
            "ayana": "在家客厅写作业",
            "akao": "在家厨房做饭",
            "chinagi": "在公司开会",
        },
    )

    text = _messages_text(_mock_extract.calls)
    # 作用域原文进了 prompt
    assert "客厅传来开关门的声音" in text
    # 每个角色 + 她此刻的客观位置都进了 prompt
    assert "ayana" in text and "在家客厅写作业" in text
    assert "akao" in text and "在家厨房做饭" in text
    assert "chinagi" in text and "在公司开会" in text


@pytest.mark.asyncio
async def test_match_input_has_no_wake_or_staleness_signal(_mock_extract):
    """匹配输入**绝不含**唤醒语义（谁该醒 / 状态多久没动 / next_wake_at）。

    在场匹配只判「事件发生在哪 + 谁在那」，不判「谁该不该被叫醒」——这是它不自锁的
    根本（不读角色活跃度）。所以喂进去的 prompt 里绝不能出现唤醒 / 叫醒 / 该醒 /
    停滞 / 多久没动 / next_wake_at / observed_at 这些会把判断带偏成「判唤醒」的词。
    """
    _mock_extract(["ayana"])

    await match_present_personas(
        scope="下课铃响，全校广播。",
        persona_locations={
            "ayana": "在学校教室上课",
            "akao": "在家睡觉",
        },
    )

    text = _messages_text(_mock_extract.calls)
    for forbidden in (
        "唤醒",
        "叫醒",
        "该醒",
        "停滞",
        "多久没动",
        "next_wake_at",
        "observed_at",
        "活跃",
    ):
        assert forbidden not in text, (
            f"在场匹配输入不得含唤醒 / 活跃度语义「{forbidden}」"
            "（它只判在场、不判该不该醒，否则自锁）"
        )


@pytest.mark.asyncio
async def test_empty_locations_short_circuits_no_llm(_mock_extract):
    """没有任何角色位置（谁都还没活过一轮）→ 直接返空、不调 LLM（省一次空调用）。

    这是机制层的「无可匹配对象就免调」短路，不是用规则替模型判在场——没有候选时
    本就无人可匹配。断言此时 LLM 一次都没被调（calls 为空）。
    """
    present = await match_present_personas(
        scope="下课铃响，全校广播。",
        persona_locations={},
    )

    assert present == []
    assert _mock_extract.calls == [], "无候选位置时不该调 LLM"


def test_prompt_leans_present_when_location_unclear():
    """位置信息不足 / 拿不准时，prompt 倾向必须偏「判她在场」（冷启动保活）。

    清库冷启动时三姐妹都还没活过一轮、没有 current_state，喂给匹配模型的是占位
    文本（「还不知道她此刻在哪」）。若 prompt 引导模型「拿不准就按常识判」，模型
    容易把「还不知道在哪」判成不在场 → 第一条客观动静投不出去 → 没人被唤醒 →
    世界永远起不来（之前 coe 反复睡死的类别）。所以 prompt 必须把默认倾向钉成
    「拿不准时倾向判在场（宁可多投一个，也别让世界因信息不足起不来）」。

    这是调 prompt 让模型有正确默认倾向，**不是**加 if / 阈值 / 规则去强制——
    在场匹配仍是纯模型判断。下面只断言 prompt 文本里有「拿不准倾向在场 / 保活」
    的意思、且没有混进确定性匹配规则。
    """
    msgs = build_presence_messages(
        scope="教室下课铃响。",
        persona_locations={
            "ayana": "（还不知道她此刻在哪——她还没活过一轮、没有可读的此刻状态。）",
        },
    )
    text = "\n".join(m.content for m in msgs)

    # 保活倾向：位置信息不足 / 拿不准时倾向判在场（而不是「拿不准就排除」）。
    assert "宁可" in text, (
        "prompt 必须把拿不准时的默认倾向钉成「宁可多投一个」（保活优先），"
        "否则冷启动无状态角色被判不在场、世界起不来"
    )
    assert "在场" in text and "倾向" in text, (
        "prompt 必须明确表达「拿不准时倾向判在场」的默认方向"
    )

    # 自查没混进确定性规则：prompt 不得引入距离阈值 / 位置枚举 / if 判定等代码侧
    # 确定性匹配语义（在场判定必须留给模型，赤尾宪法）。
    for forbidden in ("阈值", "枚举", "if ", "距离 >", "状态机"):
        assert forbidden not in text, (
            f"prompt 不得引入确定性匹配规则「{forbidden}」（在场判定必须是模型判断）"
        )


@pytest.mark.asyncio
async def test_cold_start_all_unknown_locations_still_matches(_mock_extract):
    """全员冷启动（谁都没活过一轮、位置全是占位）→ 仍把所有人喂进匹配、链路不崩。

    plumbing 级断言（在场匹配的 LLM 在此 mock 掉，测不到「模型倾向是否真偏向
    在场」——那靠 coe 真机验）：全员只有占位位置时，每个角色都进了匹配候选输入、
    匹配被正常调用一次、调用不抛。这正是清库冷启动第一条客观动静要走的路径——
    候选不能因为「全员无状态」就空掉、把世界第一条动静卡死在投不出去。
    """
    _mock_extract(["chinagi", "ayana", "akao"])
    placeholder = "（还不知道她此刻在哪——她还没活过一轮、没有可读的此刻状态。）"

    present = await match_present_personas(
        scope="清晨厨房飘来煎蛋的香味。",
        persona_locations={
            "chinagi": placeholder,
            "ayana": placeholder,
            "akao": placeholder,
        },
    )

    # 链路通：匹配被调一次，全员都进了候选输入（没人因无状态被排除）。
    text = _messages_text(_mock_extract.calls)
    for persona_id in ("chinagi", "ayana", "akao"):
        assert persona_id in text, (
            f"冷启动全员无状态时 {persona_id} 必须仍进匹配候选（否则世界起不来）"
        )
    # 候选齐全、模型返谁就投谁（这里 mock 返全员）。
    assert present == ["chinagi", "ayana", "akao"]


@pytest.mark.asyncio
async def test_verdict_present_filtered_to_known_personas(_mock_extract):
    """模型若返了不在候选里的 persona_id，过滤掉——只投真候选里的人。

    在场匹配的判定来自模型自然语言判断，可能偶发返一个候选外的 id（幻觉）。投递
    必须只落真候选里的人，候选外的 id 丢弃（不凭空给一个不存在的人投信）。这是
    投递正确性兜底，不是替模型判在场。
    """
    _mock_extract(["ayana", "顾舟"])  # 顾舟不在候选里（幻觉）

    present = await match_present_personas(
        scope="客厅有动静。",
        persona_locations={"ayana": "在家客厅", "akao": "在公司"},
    )

    assert present == ["ayana"], "候选外的幻觉 id（顾舟）必须被过滤掉"
