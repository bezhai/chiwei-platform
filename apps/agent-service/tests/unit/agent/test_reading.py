"""阅读 agent 契约 — 读一程、揉出新印象（读小说 Task 2）.

一个简单 ReAct agent（deepseek-v4-flash，唯一工具 ``read(page_num)``）：外壳喂它
[这本书当前印象 + 从第几页接着读]，它往后读一程、最终输出 = 揉好的新印象正文。本文件
钉住阅读 agent 的机制层硬约束（照 sediment 的范式）：

  * **唯一工具 read(page_num)**：内部调 Task 1 的 ``read_page``；**进度从 read() 调用
    机制派生**——记下本程实际 read 过的最大页号（+1 = 下次从第几页接着读），绝不靠
    agent 文字自报（项目红线）。读到 ``read_page`` 返回 None 即到书尾。
  * **一程靠机制安全阀收口**：硬 timeout（asyncio.wait_for）+ recursion_limit + 最多
    read() 次数上限 + 读到书尾。**不设语义页数预算**（用户明确「不规定读多少」）。
  * **deepseek-v4-flash**：AgentConfig 指向它（model 别名解析见 app/agent/models.py）。
  * **失败 fail-soft**：超时 / 抛错 / 空产出 → 返回 None（外壳据此不动印象 / 页号，
    本程不算、她可重读）。绝不写半截脏印象。
  * **成本独立入账**：collect_usage 包住 LLM 调用、record_round_cost(actor=
    ``{persona}:reading``)，别混进 life 本体账。

阅读 agent 的 LLM 调用全部 mock 掉（测的是工具机制 + 收口 + fail-soft），绝不真打模型。
"""

from __future__ import annotations

import asyncio

import pytest

import app.agent.reading as reading_mod
from app.agent.neutral import Message, Role
from app.agent.reading import (
    ReadingResult,
    run_reading_round,
)
from app.agent.trace import _accumulate_usage

_LANE = "coe-t2"
_PERSONA = "akao"
_BOOK = "book-abc"
_ROUND = "reading-round-xyz"


@pytest.fixture(autouse=True)
def stub_read_page(monkeypatch):
    """read_page 打桩：一本 5 页的书（page 0..4），越界返回 None（到书尾）。"""
    pages = {i: f"第 {i} 页正文。" for i in range(5)}

    async def fake_read_page(*, lane, book_id, page_num):
        return pages.get(page_num)

    monkeypatch.setattr(reading_mod, "read_page", fake_read_page)
    return pages


@pytest.fixture(autouse=True)
def stub_book_meta(monkeypatch):
    """find_book_meta 打桩：默认这本书 total_pages=5（对齐 stub_read_page 的 0..4）。

    测试可在自己 block 里改 holder["total_pages"] / 设 holder["meta_none"]=True
    （验数据缺损 / orphan 路径）。
    """
    from app.domain.book import BookMeta

    holder = {"total_pages": 5, "meta_none": False}

    async def fake_find_book_meta(*, lane, book_id):
        if holder["meta_none"]:
            return None
        return BookMeta(
            lane=lane, book_id=book_id, persona_id=_PERSONA, title="夏天的书",
            total_pages=holder["total_pages"], content_hash="h",
            ingested_at="2026-06-23T09:00:00+08:00",
        )

    monkeypatch.setattr(reading_mod, "find_book_meta", fake_find_book_meta)
    return holder


@pytest.fixture(autouse=True)
def cost_records(monkeypatch):
    """record_round_cost 打桩：快照本程成本入账。"""
    costs: list[dict] = []

    async def fake_record_round_cost(**kwargs):
        costs.append({**kwargs, "usage": dict(kwargs["usage"])})

    monkeypatch.setattr(reading_mod, "record_round_cost", fake_record_round_cost)
    return costs


@pytest.fixture(autouse=True)
def stub_persona(monkeypatch):
    """load_persona 打桩（印象 prompt 的 prompt_vars 从这取）。"""
    from app.memory._persona import PersonaContext

    async def fake_load_persona(persona_id):
        return PersonaContext(
            persona_id=persona_id, display_name="赤尾", persona_lite="人设速写"
        )

    monkeypatch.setattr(reading_mod, "load_persona", fake_load_persona)


def _install_agent(
    monkeypatch,
    *,
    read_calls: list[int] | None = None,
    text: str = "读完这一程，我心里多了点什么。",
    usage: dict | None = None,
    exc: Exception | None = None,
    delay: float = 0.0,
):
    """把阅读模块的 ``Agent`` 换成桩：模拟模型让 agent 调若干次 read、再产出印象正文。

    ``read_calls``：模拟模型这一程要 read 的页号序列（按序调 read 工具）。run 内部会真的
    调到 build 出来的 read 工具（从而真正驱动「进度从 read 机制派生」），所以桩里直接
    invoke 那个工具。
    """
    captured: dict = {"cfg": None, "tools": None, "runs": 0}

    class _FakeAgent:
        def __init__(self, cfg, *, tools=None, **kwargs):
            captured["cfg"] = cfg
            captured["tools"] = tools

        async def run(self, messages, *, prompt_vars=None, context=None,
                      session_id=None, max_retries=2):
            captured["runs"] += 1
            captured["prompt_vars"] = prompt_vars
            captured["context"] = context
            captured["max_retries"] = max_retries
            if delay:
                await asyncio.sleep(delay)
            # 模拟模型在循环里调 read 工具若干次（真的驱动外壳的 read 工具，让进度从
            # read 机制派生）。
            if read_calls is not None:
                read_tool = {t.name: t for t in (captured["tools"] or [])}["read"]
                for pn in read_calls:
                    await read_tool.invoke({"page_num": pn})
            if usage is not None:
                _accumulate_usage(usage)
            if exc is not None:
                raise exc
            return Message(role=Role.ASSISTANT, content=text)

    monkeypatch.setattr(reading_mod, "Agent", _FakeAgent)
    return captured


async def _run(start_page=0, prior=None):
    return await run_reading_round(
        lane=_LANE,
        persona_id=_PERSONA,
        book_id=_BOOK,
        book_title="夏天的书",
        prior_impression=prior,
        start_page=start_page,
        round_id=_ROUND,
    )


# ---------------------------------------------------------------------------
# 模型 / 工具契约
# ---------------------------------------------------------------------------


def test_reading_config_points_at_deepseek_flash():
    """阅读 agent 的 AgentConfig 指向 deepseek-v4-flash。"""
    assert reading_mod._READING_CFG.model_id == "deepseek-v4-flash"


def test_reading_config_pins_prompt_id():
    """印象生成 prompt 走 Langfuse prompt id（系统人设走 prompt）。"""
    assert reading_mod._READING_CFG.prompt_id == "book_reading_impression"


async def test_reading_agent_has_only_read_tool(monkeypatch):
    """唯一工具 read（不给它 act / chat / 任何写工具——它只读、只产出印象）。"""
    captured = _install_agent(monkeypatch, read_calls=[0])
    await _run()
    names = {t.name for t in captured["tools"]}
    assert names == {"read"}


# ---------------------------------------------------------------------------
# 进度从 read() 机制派生（不靠 agent 文字自报）
# ---------------------------------------------------------------------------


async def test_progress_is_contiguous_reading_frontier(monkeypatch):
    """读到第几页 = 从本程起始页起连续成功读到的前沿（不是最大页号）。"""
    # 模型从起始页 0 连续 read 了 0,1,2 三页 → 连续前沿 = 3 → pages_read = 3
    _install_agent(monkeypatch, read_calls=[0, 1, 2])
    result = await _run(start_page=0)
    assert isinstance(result, ReadingResult)
    assert result.pages_read == 3, "连续前沿 = 起始页 + 连续读到的页数"
    assert result.finished is False, "没读到书尾"
    assert result.impression == "读完这一程，我心里多了点什么。"


async def test_jump_read_does_not_skip_middle_content(monkeypatch):
    """修复 A：乱序 / 跳页 read 不让进度跳过中间没读的内容。

    模型从起始页 0 起：读 0（前沿→1）→ 跳读 2（被挡、不接受）→ 回读 1（前沿→2）。
    只有连续读到的前沿算数：进度 = 2，绝不因为「碰过页 2」就跳到 3 把中间漏掉。
    """
    _install_agent(monkeypatch, read_calls=[0, 2, 1])
    result = await _run(start_page=0)
    assert result.pages_read == 2, "跳页的 read 被挡，进度只按连续前沿派生、不跳过中间"
    assert result.finished is False


async def test_jump_read_is_rejected_with_guidance(monkeypatch):
    """修复 A：非连续 / 跳页的 read 调用被挡（喂回引导让它读下一页），不静默接受。"""
    captured = _install_agent(monkeypatch, read_calls=[0])
    await _run(start_page=0)
    read_tool = {t.name: t for t in captured["tools"]}["read"]
    # 前沿现在在第 1 页（读过 0）。跳读第 3 页应被挡、回引导文字、不喂正文。
    out = await read_tool.invoke({"page_num": 3})
    assert "第 3 页正文" not in out, "跳页不喂正文"
    assert "1" in out, "引导里指出该读的下一页（第 1 页）"


async def test_progress_ignores_agent_text_claims(monkeypatch):
    """页号绝不从 agent 文字抠：它在印象里胡说「我读到第 99 页」也只按真实 read 派生。"""
    _install_agent(
        monkeypatch, read_calls=[0, 1], text="我一口气读到了第 99 页！"
    )
    result = await _run(start_page=0)
    assert result.pages_read == 2, "只认连续读到的前沿，不认文字自报的 99"


async def test_no_read_calls_returns_none(monkeypatch):
    """修复 A：一次 read 都没真读到（模型直接产出）→ fail-soft 返回 None，不提交脏印象。"""
    _install_agent(monkeypatch, read_calls=[], text="（没读，凭旧印象写了点）")
    result = await _run(start_page=3)
    assert result is None, "前沿没推进（一页没真读）→ 不提交、印象 / 页号都不动"


async def test_only_rejected_reads_returns_none(monkeypatch):
    """修复 A：调了 read 但全是跳页（前沿一页没推进）→ fail-soft 返回 None。"""
    # 起始页 0，但模型只跳读高页号（都被挡），连续前沿始终停在 0 → 一页没真读
    _install_agent(monkeypatch, read_calls=[2, 4, 3], text="（瞎翻了几页没读进去）")
    result = await _run(start_page=0)
    assert result is None, "前沿没推进 = 没真读到 → 不提交"


# ---------------------------------------------------------------------------
# 读到书尾（read_page 返 None）→ finished
# ---------------------------------------------------------------------------


async def test_reaching_book_end_sets_finished(monkeypatch):
    """读到书尾（read 越界页返 None 且页号 >= total_pages）→ finished=True、页号不越界。"""
    # 书 5 页（0..4），total_pages=5。模型从第 3 页连续读到第 5 页（越界）→ 第 5 页 = 真 EOF
    _install_agent(monkeypatch, read_calls=[3, 4, 5])
    result = await _run(start_page=3)
    assert result.finished is True, "read 到 None 且页号已达 total_pages = 真书尾"
    # 连续前沿读到第 4 页 → pages_read = 5（= total_pages），不越界到 6
    assert result.pages_read == 5, "页号夹到书尾、不越界"


async def test_data_gap_in_range_not_treated_as_eof(monkeypatch, stub_read_page, stub_book_meta):
    """修复 B：范围内某页 read_page 返 None（数据缺损）→ 不置 finished、不当书尾。

    书 total_pages=10 但只有 0..4 有正文（中间缺页 = 书被删 / 部分入库失败）。从第 0 页
    连续读，读到第 5 页返 None——但 5 < total_pages=10，是数据缺损不是真 EOF。不置
    finished、不把这本因为中间缺页误判成「读完了」。前沿停在 5（连续读到 0..4），但因
    这一程没读到真书尾 → finished=False。
    """
    stub_book_meta["total_pages"] = 10  # 声明 10 页，但 stub_read_page 只有 0..4
    _install_agent(monkeypatch, read_calls=[0, 1, 2, 3, 4, 5])
    result = await _run(start_page=0)
    assert result is not None, "读到了 0..4 五页真正文，本程算数"
    assert result.finished is False, "范围内缺页是数据缺损，绝不当书尾置 finished"
    assert result.pages_read == 5, "连续前沿读到第 4 页 → 下次从第 5 页接着重试"


async def test_meta_missing_in_reading_round_returns_none(monkeypatch, stub_book_meta):
    """修复 B/orphan：跑阅读一程时书 meta 查不到（书被删 / 入库回滚）→ fail-soft 返回 None。

    没有 total_pages 就无法安全区分 EOF 与缺页，也读不了一本不存在的书 → 不提交。
    """
    stub_book_meta["meta_none"] = True
    _install_agent(monkeypatch, read_calls=[0, 1])
    result = await _run(start_page=0)
    assert result is None, "meta 查不到 → 不提交、印象 / 页号都不动"


# ---------------------------------------------------------------------------
# 机制安全阀：最多 read 次数上限
# ---------------------------------------------------------------------------


async def test_read_call_cap_enforced(monkeypatch):
    """超过最多 read 次数上限后，read 工具拒绝继续喂正文（机制安全阀，不靠语义页数预算）。"""
    monkeypatch.setattr(reading_mod, "MAX_READ_CALLS", 2)
    captured = _install_agent(monkeypatch, read_calls=[0, 1, 2, 3])
    await _run(start_page=0)
    # 前 2 次正常喂正文，第 3、4 次被上限拦（返回到顶提示而非正文）——但进度仍只按
    # 真实喂出正文的页派生：最大喂出页是 1 → pages_read = 2。
    read_tool = {t.name: t for t in captured["tools"]}["read"]
    over = await read_tool.invoke({"page_num": 9})
    assert "够了" in over or "停" in over or "上限" in over, "触顶给停读提示"


# ---------------------------------------------------------------------------
# 成本独立入账
# ---------------------------------------------------------------------------


async def test_cost_lands_on_reading_actor(monkeypatch, cost_records):
    """阅读 agent 成本入独立 actor（{persona}:reading），不混进 life 本体账。"""
    _install_agent(
        monkeypatch, read_calls=[0], usage={"input": 50, "output": 5, "total": 55}
    )
    await _run()
    assert len(cost_records) == 1
    rec = cost_records[0]
    assert rec["lane"] == _LANE
    assert rec["actor"] == f"{_PERSONA}:reading"
    assert rec["round_id"] == _ROUND
    assert rec["usage"]["input"] == 50


# ---------------------------------------------------------------------------
# fail-soft：超时 / 抛错 / 空产出 → 返回 None（外壳不动印象 / 页号）
# ---------------------------------------------------------------------------


async def test_llm_failure_returns_none(monkeypatch):
    """LLM 抛错 → fail-soft 返回 None（外壳据此不动印象 / 页号，她可重读）。"""
    _install_agent(monkeypatch, read_calls=[0], exc=RuntimeError("llm down"))
    result = await _run()
    assert result is None


async def test_hard_timeout_returns_none(monkeypatch):
    """硬超时（asyncio.wait_for 包住 run）→ fail-soft 返回 None。"""
    _install_agent(monkeypatch, read_calls=[0], delay=5.0)
    monkeypatch.setattr(reading_mod, "READING_TIMEOUT_SECONDS", 0.05)
    result = await _run()
    assert result is None


async def test_empty_output_returns_none(monkeypatch):
    """空产出 → fail-soft 返回 None（绝不写半截 / 空印象，那是真失忆）。"""
    _install_agent(monkeypatch, read_calls=[0, 1], text="   \n  ")
    result = await _run()
    assert result is None


def test_timeout_below_durable_retry_safety():
    """硬超时是有限值（机制安全阀存在）。"""
    assert reading_mod.READING_TIMEOUT_SECONDS > 0
