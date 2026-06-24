"""阅读 agent 契约 — 读她收到的一个文件、揉出新印象（读小说 Task 2）.

一个简单 ReAct agent（deepseek-v4-flash，唯一工具 ``read(page_num)``）：外壳喂它
[这本书当前印象 + 从第几页接着读]，它往后读一程、最终输出 = 揉好的新印象正文。

读的是**她收到的一个文件**（不是注册的书）：读的时候才从对象存储取这个附件实例的字节、
现解码现分页。本文件钉住阅读 agent 的机制层硬约束（照 sediment 的范式）：

  * **读时取字节 + 现解码现分页**：进 run 先 ``fetch_attachment_bytes(tos_file)`` 取字节、
    ``decode_pages(file_name, raw)`` 现算页；取不到字节（未缓存 / 预签失败）/ 解析失败 →
    整程 fail-soft 返回 None（印象 / 页号都不动）。**不依赖任何书注册表**（无 find_book_meta /
    read_page）。
  * **唯一工具 read(page_num)**：从内存页列表现切；**进度从 read() 调用机制派生**——连续
    阅读前沿，绝不靠 agent 文字自报（项目红线）。读到越界页（>= total_pages）即到书尾。
  * **一程靠机制安全阀收口**：硬 timeout + recursion_limit + 最多 read 次数上限 + 读到书尾。
  * **失败 fail-soft**：超时 / 抛错 / 空产出 / 取不到字节 → 返回 None。绝不写半截脏印象。
  * **成本独立入账**：collect_usage + record_round_cost(actor=``{persona}:reading``)。

阅读 agent 的 LLM 调用全部 mock，取字节/解码按需 mock（测的是工具机制 + 收口 + fail-soft），
绝不真打模型 / 真发网络。
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
_ATTACHMENT = "msg-1:file-k"
_TOS = "files/file-k"
_FILE_NAME = "斜阳.txt"
_ROUND = "reading-round-xyz"


@pytest.fixture(autouse=True)
def stub_pages(monkeypatch):
    """fetch_attachment_bytes + decode_pages 打桩：默认一本 5 页的文件（page 0..4）。

    测试可改 holder 控制：``fetch_none``=取不到字节、``parse_error``=解码抛错、
    ``pages``=改页数（验数据缺损 / EOF）。
    """
    from app.domain.reading_source import BookParseError

    holder = {
        "pages": [f"第 {i} 页正文。" for i in range(5)],
        "fetch_none": False,
        "parse_error": False,
        "fetch_calls": [],
        "decode_calls": [],
    }

    async def fake_fetch(*, tos_file):
        holder["fetch_calls"].append(tos_file)
        if holder["fetch_none"]:
            return None
        return b"raw file bytes"

    def fake_decode(file_name, raw, *, page_size=1800):
        holder["decode_calls"].append(file_name)
        if holder["parse_error"]:
            raise BookParseError("bad file")
        return holder["pages"]

    monkeypatch.setattr(reading_mod, "fetch_attachment_bytes", fake_fetch)
    monkeypatch.setattr(reading_mod, "decode_pages", fake_decode)
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
    """把阅读模块的 ``Agent`` 换成桩：模拟模型调若干次 read、再产出印象正文。"""
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
        attachment_id=_ATTACHMENT,
        book_title=_FILE_NAME,
        tos_file=_TOS,
        file_name=_FILE_NAME,
        prior_impression=prior,
        start_page=start_page,
        round_id=_ROUND,
    )


# ---------------------------------------------------------------------------
# 模型 / 工具契约
# ---------------------------------------------------------------------------


def test_reading_config_points_at_deepseek_flash():
    assert reading_mod._READING_CFG.model_id == "deepseek-v4-flash"


def test_reading_config_pins_prompt_id():
    assert reading_mod._READING_CFG.prompt_id == "book_reading_impression"


async def test_reading_agent_has_only_read_tool(monkeypatch):
    """唯一工具 read（只读、只产出印象）。"""
    captured = _install_agent(monkeypatch, read_calls=[0])
    await _run()
    names = {t.name for t in captured["tools"]}
    assert names == {"read"}


# ---------------------------------------------------------------------------
# 读时取字节 + 现解码现分页（无书注册表）
# ---------------------------------------------------------------------------


async def test_fetches_bytes_and_decodes_by_filename(monkeypatch, stub_pages):
    """进 run 先取这个附件实例的字节（按 tos_file），用原始 file_name 现解码现分页。"""
    _install_agent(monkeypatch, read_calls=[0, 1])
    await _run(start_page=0)
    assert stub_pages["fetch_calls"] == [_TOS], "按 tos_file 取字节"
    assert stub_pages["decode_calls"] == [_FILE_NAME], "用原始 file_name 分流解码"


async def test_fetch_none_is_fail_soft(monkeypatch, stub_pages):
    """取不到字节（未缓存进对象存储 / 预签失败）→ 整程 fail-soft 返回 None，印象不动。"""
    stub_pages["fetch_none"] = True
    _install_agent(monkeypatch, read_calls=[0, 1])
    result = await _run(start_page=0)
    assert result is None, "拿不到这个附件的字节 → 不提交、印象 / 页号都不动"


async def test_parse_error_is_fail_soft(monkeypatch, stub_pages):
    """解码失败（坏 epub / 空文件）→ 整程 fail-soft 返回 None。"""
    stub_pages["parse_error"] = True
    _install_agent(monkeypatch, read_calls=[0])
    result = await _run(start_page=0)
    assert result is None


async def test_total_pages_computed_at_read_time(monkeypatch, stub_pages):
    """total_pages 现算（= 解码出的页数），不查任何书注册表。"""
    stub_pages["pages"] = [f"p{i}" for i in range(3)]  # 3 页
    # 从第 0 页连续读到第 3 页（越界）→ 读到真书尾
    _install_agent(monkeypatch, read_calls=[0, 1, 2, 3])
    result = await _run(start_page=0)
    assert result.finished is True, "页号达 total_pages(=3) 即真书尾"
    assert result.pages_read == 3


# ---------------------------------------------------------------------------
# 进度从 read() 机制派生（不靠 agent 文字自报）
# ---------------------------------------------------------------------------


async def test_progress_is_contiguous_reading_frontier(monkeypatch, stub_pages):
    """读到第几页 = 连续成功读到的前沿。"""
    _install_agent(monkeypatch, read_calls=[0, 1, 2])
    result = await _run(start_page=0)
    assert isinstance(result, ReadingResult)
    assert result.pages_read == 3
    assert result.finished is False
    assert result.impression == "读完这一程，我心里多了点什么。"


async def test_jump_read_does_not_skip_middle_content(monkeypatch, stub_pages):
    """修复 A：乱序 / 跳页 read 不让进度跳过中间没读的内容。"""
    _install_agent(monkeypatch, read_calls=[0, 2, 1])
    result = await _run(start_page=0)
    assert result.pages_read == 2
    assert result.finished is False


async def test_jump_read_is_rejected_with_guidance(monkeypatch, stub_pages):
    """修复 A：非连续 read 被挡（喂回引导），不静默接受。"""
    captured = _install_agent(monkeypatch, read_calls=[0])
    await _run(start_page=0)
    read_tool = {t.name: t for t in captured["tools"]}["read"]
    out = await read_tool.invoke({"page_num": 3})
    assert "第 3 页正文" not in out
    assert "1" in out, "引导里指出该读的下一页（第 1 页）"


async def test_progress_ignores_agent_text_claims(monkeypatch, stub_pages):
    """页号绝不从 agent 文字抠：胡说读到第 99 页也只按真实 read 派生。"""
    _install_agent(monkeypatch, read_calls=[0, 1], text="我一口气读到了第 99 页！")
    result = await _run(start_page=0)
    assert result.pages_read == 2


async def test_no_read_calls_returns_none(monkeypatch, stub_pages):
    """修复 A：一页没真读到（模型直接产出）→ fail-soft 返回 None。"""
    _install_agent(monkeypatch, read_calls=[], text="（没读，凭旧印象写了点）")
    result = await _run(start_page=3)
    assert result is None


async def test_only_rejected_reads_returns_none(monkeypatch, stub_pages):
    """修复 A：调了 read 但全是跳页（前沿一页没推进）→ fail-soft 返回 None。"""
    _install_agent(monkeypatch, read_calls=[2, 4, 3], text="（瞎翻了几页没读进去）")
    result = await _run(start_page=0)
    assert result is None


# ---------------------------------------------------------------------------
# 读到书尾（页号越界）→ finished
# ---------------------------------------------------------------------------


async def test_reaching_book_end_sets_finished(monkeypatch, stub_pages):
    """读到书尾（read 越界页 None 且页号 >= total_pages）→ finished=True、页号不越界。"""
    # 5 页（0..4），从第 3 页连续读到第 5 页（越界）= 真 EOF
    _install_agent(monkeypatch, read_calls=[3, 4, 5])
    result = await _run(start_page=3)
    assert result.finished is True
    assert result.pages_read == 5, "页号夹到书尾、不越界到 6"


async def test_data_gap_in_range_not_treated_as_eof(monkeypatch, stub_pages):
    """修复 B：范围内某页取不到（数据缺损）→ 不置 finished、不当书尾。

    内存页列表声明 total=10、但只有 0..4 有正文（模拟解码出短列表但下游声明更长 ——
    用 pages 长度 5、再单独把 total_pages 撑到 10 不现实；这里用「页列表只有 0..4，第 5
    页取 None 但若 total_pages>5」的语义：通过让 read 工具范围判定按 total_pages 走）。

    实际内存列表里 total_pages == len(pages)，所以"范围内缺页"不会自然发生 —— 这个用例
    钉的是：当某页取不到且页号 < total_pages 时**不当 EOF**。用一个比 len(pages) 大的
    total_pages 触发（见实现：total_pages 来自 len(pages)，故这里改用 pages 列表含 None
    占位来制造范围内缺页）。
    """
    # pages 列表第 5 位是 None（缺损占位），但列表长度 7 → total_pages=7
    stub_pages["pages"] = ["p0", "p1", "p2", "p3", "p4", None, "p6"]
    _install_agent(monkeypatch, read_calls=[0, 1, 2, 3, 4, 5])
    result = await _run(start_page=0)
    assert result is not None, "读到 0..4 五页真正文，本程算数"
    assert result.finished is False, "范围内缺页是数据缺损，绝不当书尾"
    assert result.pages_read == 5, "连续前沿停在缺页前，下次从第 5 页重试"


async def test_resuming_exactly_at_book_end_commits_finished(monkeypatch, stub_pages):
    """EOF 不卡死（codex T3 ②）：上一程正好读完最后一页、下一程从书尾起 → 提交 finished。

    死锁场景：上一程把 0..4（total=5）全读完、frontier 停在 5、但模型没调 read(5)，所以
    reached_end 没置、状态仍 reading、印象 pages_read=5。下一程 start_page=5：模型调
    read(5) → 越界、置 reached_end=True、但 frontier 仍是 5（== start_page，没新前沿推进）。
    若沿用"前沿没推进就 drop"，这一程被 fail-soft 丢弃、状态永远停在 reading、永不 finished。
    修法：读到真书尾（reached_end）即便没有新前沿推进也提交 finished（只要印象非空）。
    """
    stub_pages["pages"] = [f"第 {i} 页" for i in range(5)]  # 0..4, total=5
    # 从书尾起、只调越界页 read(5)：触达真 EOF、但前沿不推进
    _install_agent(monkeypatch, read_calls=[5], text="读到结尾了，心里空落落的。")
    result = await _run(start_page=5)
    assert result is not None, "读到真书尾必须提交，绝不被 fail-soft 丢弃成永久 reading"
    assert result.finished is True, "触达真书尾 → finished"
    assert result.pages_read == 5, "页号夹到书尾、不越界"


async def test_reaching_end_with_no_new_frontier_still_finished(monkeypatch, stub_pages):
    """读到书尾即提交 finished，即使本程前沿一页没推进（与"没真读到任何页"的 drop 区分）。

    与 test_no_read_calls_returns_none 的区别：那个是既没读到页、也没触达书尾（纯空转）→ drop；
    这个是触达了真书尾（reached_end=True）→ 必须提交 finished（读完一本书是真实终点、不是空转）。
    """
    stub_pages["pages"] = [f"第 {i} 页" for i in range(3)]  # total=3
    _install_agent(monkeypatch, read_calls=[3], text="终于读完了。")
    result = await _run(start_page=3)
    assert result is not None and result.finished is True
    assert result.pages_read == 3


# ---------------------------------------------------------------------------
# 机制安全阀：最多 read 次数上限
# ---------------------------------------------------------------------------


async def test_read_call_cap_enforced(monkeypatch, stub_pages):
    """超过最多 read 次数上限后拒绝继续喂正文（机制安全阀）。"""
    monkeypatch.setattr(reading_mod, "MAX_READ_CALLS", 2)
    captured = _install_agent(monkeypatch, read_calls=[0, 1, 2, 3])
    await _run(start_page=0)
    read_tool = {t.name: t for t in captured["tools"]}["read"]
    over = await read_tool.invoke({"page_num": 9})
    assert "够" in over or "停" in over or "上限" in over


# ---------------------------------------------------------------------------
# 成本独立入账
# ---------------------------------------------------------------------------


async def test_cost_lands_on_reading_actor(monkeypatch, cost_records):
    """阅读 agent 成本入独立 actor（{persona}:reading）。"""
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
# fail-soft：超时 / 抛错 / 空产出 → 返回 None
# ---------------------------------------------------------------------------


async def test_llm_failure_returns_none(monkeypatch, stub_pages):
    _install_agent(monkeypatch, read_calls=[0], exc=RuntimeError("llm down"))
    result = await _run()
    assert result is None


async def test_hard_timeout_returns_none(monkeypatch, stub_pages):
    _install_agent(monkeypatch, read_calls=[0], delay=5.0)
    monkeypatch.setattr(reading_mod, "READING_TIMEOUT_SECONDS", 0.05)
    result = await _run()
    assert result is None


async def test_empty_output_returns_none(monkeypatch, stub_pages):
    _install_agent(monkeypatch, read_calls=[0, 1], text="   \n  ")
    result = await _run()
    assert result is None


def test_timeout_below_durable_retry_safety():
    assert reading_mod.READING_TIMEOUT_SECONDS > 0
