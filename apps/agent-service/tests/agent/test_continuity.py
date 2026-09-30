"""跨轮连续的上下文，基础层那一半：存下去、裁一遍、插入带时刻的标记消息。

这一层给任何 App 的 agent 用，所以这里的用例不借任何一个 App 的东西：工具名、阈值、
状态文字都是用例自己编的。钉的是五条：

  1. **不认识任何业务上的时间。** 清理点是一张从 Unix 纪元起算的固定网格，不按谁的
     "一天"对齐；标记消息里的时刻沿用调用方给的时区。
  2. **不 import 任何 App 的代码。**
  3. **写入带版本 CAS。** 别人在中间写过就抛 :class:`TranscriptConflict`，一行都不写；
     写入跑在调用方的事务里。
  4. **阈值和素材表都由调用方给**，这一层一个默认值都没有，也不读任何配置。
  5. **裁剪的规则对任意工具名成立**：素材载荷过期换短语、整组过期整组删、图片只活在
     最新那一代、上一轮没存下来就补一条缺口标记消息、硬顶从最老的组开始丢。

她那一侧怎么用这一层（她的阈值、她的素材表、她一轮跑下来存了什么）在
``tests/living/test_context_trim.py`` 和 ``tests/living/test_continuity.py``。
"""

from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import inspect
from pathlib import Path

import pytest

from app.agent.continuity import (
    CHECKPOINT_HEAD,
    GAP_HEAD,
    MATERIAL_TRIMMED,
    PICTURE_TRIMMED,
    TranscriptConflict,
    TrimPolicy,
    commit_transcript,
    estimate_tokens,
    next_transcript,
    trim_for_round,
)
from app.agent.neutral import ContentBlock, Message, Role, ToolCall
from app.agent.session import load_session
from app.domain.session_transcript import SessionTranscript
from tests.runtime.conftest import migrate, test_db, test_db_dsn  # noqa: F401

_CST = dt.timezone(dt.timedelta(hours=8))
_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)

POLICY = TrimPolicy(
    material_minutes=60,
    own_minutes=240,
    cleanup_minutes=60,
    hard_cap_tokens=200_000,
    trim_target_tokens=100_000,
)
MATERIAL = frozenset({"fetch_page"})
STATE = "状态：在工位上，手边摊着一份报告。"


def _at(hour: int, minute: int = 0, day: int = 25) -> dt.datetime:
    return dt.datetime(2026, 7, day, hour, minute, tzinfo=_CST)


def _call(name: str, call_id: str) -> Message:
    return Message(
        role=Role.ASSISTANT,
        content="",
        tool_calls=[ToolCall(id=call_id, name=name, arguments={})],
    )


def _result(call_id: str, content) -> Message:
    return Message(role=Role.TOOL, content=content, tool_call_id=call_id)


def _said(text: str) -> Message:
    return Message(role=Role.ASSISTANT, content=text)


def _marker_time(message: Message, head: str) -> dt.datetime:
    text = message.text()
    assert text.startswith(head), f"不是这种标记消息：{text[:40]!r}"
    return dt.datetime.fromisoformat(text[len(head) : text.index("】")])


def _round(
    history: list[Message],
    at: dt.datetime,
    *produced: Message,
    policy: TrimPolicy = POLICY,
    material_tools: frozenset[str] = MATERIAL,
) -> list[Message]:
    fed = trim_for_round(
        history, now=at, state=STATE, policy=policy, material_tools=material_tools
    )
    round_input = Message(role=Role.USER, content=f"现在 {at:%H:%M}。")
    return next_transcript(fed, [round_input, *produced], policy=policy)


def _play(
    *,
    start: dt.datetime,
    until: dt.datetime,
    events: dict[str, list[Message]],
    material_tools: frozenset[str] = MATERIAL,
) -> list[Message]:
    """每 10 分钟一轮跑过去；``events`` 按 ``"13:30"`` 这样的钟点挂那一轮的产出。"""
    ctx: list[Message] = []
    at = start
    while at <= until:
        produced = events.get(f"{at:%H:%M}") or [_said("继续")]
        ctx = _round(ctx, at, *produced, material_tools=material_tools)
        at += dt.timedelta(minutes=10)
    return ctx


def _tool_payload(ctx: list[Message], call_id: str) -> str:
    got = [m for m in ctx if m.role is Role.TOOL and m.tool_call_id == call_id]
    assert len(got) == 1, f"{call_id} 的结果不在了或者不止一条：{len(got)}"
    return got[0].text()


def _orphans(messages: list[Message]) -> list[str]:
    answered = {m.tool_call_id for m in messages if m.role is Role.TOOL}
    return [tc.id for m in messages for tc in m.tool_calls if tc.id not in answered]


# ---------------------------------------------------------------------------
# 一 · 清理点是一张固定网格，不按谁的"一天"
# ---------------------------------------------------------------------------


def test_the_cleanup_grid_counts_from_the_epoch_not_from_anyones_day():
    """周期不整除一天时最能看出网格从哪儿起算：45 分钟一格，跨过凌晨 4 点照样是 45 分钟。

    按某个"一天从 04:00 起"对齐的话，04:10 那一轮的标记消息时刻会是 04:00 —— 离纪元
    1200 分钟，不是 45 的倍数。
    """
    every_45 = dataclasses.replace(POLICY, cleanup_minutes=45)
    for now in (_at(3, 50), _at(4, 10), _at(4, 40), _at(0, 5), _at(23, 59)):
        fed = trim_for_round(
            [], now=now, state=STATE, policy=every_45, material_tools=MATERIAL
        )
        at = _marker_time(fed[0], CHECKPOINT_HEAD)
        assert (at - _EPOCH) % dt.timedelta(minutes=45) == dt.timedelta(0), (
            f"{now} 的清理点 {at} 不在从纪元起算的 45 分钟网格上"
        )
        assert at <= now < at + dt.timedelta(minutes=45)


def test_an_hourly_grid_lands_on_whole_hours_in_the_callers_zone():
    """一小时一格落在整点上，标记消息里的时刻沿用调用方给的时区。"""
    fed = trim_for_round(
        [], now=_at(14, 20), state=STATE, policy=POLICY, material_tools=MATERIAL
    )

    assert fed[0].text().startswith(f"{CHECKPOINT_HEAD}2026-07-25T14:00:00+08:00】")


def test_the_cleanup_line_never_goes_backwards():
    """清理点只往前走。倒回去的话"跨没跨过清理点"会判错，一整段时间一条标记消息都不插入。"""
    last = None
    at = _at(0, 0)
    while at < _at(0, 0, day=27):
        fed = trim_for_round(
            [], now=at, state=STATE, policy=POLICY, material_tools=MATERIAL
        )
        now = _marker_time(fed[0], CHECKPOINT_HEAD)
        assert last is None or now >= last, f"{at} 的清理点 {now} 比上一个 {last} 还早"
        last = now
        at += dt.timedelta(minutes=7)


# ---------------------------------------------------------------------------
# 二 · 基础层不 import 任何 App
# ---------------------------------------------------------------------------


def test_the_layer_imports_no_app():
    """基础层给 world 和 life 共用，它 import 任何一边都会把那一边的概念带进另一边。"""
    import app.agent.continuity as mod

    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported.extend(a.name for a in node.names)
    apps = [m for m in imported if m.startswith(("app.living", "app.world", "app.wiring"))]
    assert apps == [], f"基础层 import 了 App 的代码：{apps}"


# ---------------------------------------------------------------------------
# 三 · 写入：版本 CAS，跑在调用方的事务里
# ---------------------------------------------------------------------------


@pytest.fixture
async def transcript_db(test_db):  # noqa: F811
    await migrate(SessionTranscript, test_db)
    yield test_db


@pytest.mark.integration
async def test_a_write_lands_as_the_next_version(transcript_db):
    from app.data.session import get_session

    async with get_session() as s:
        await commit_transcript("coe-x:someone", [_said("第一版")], expected_ver=0, session=s)

    loaded, ver = await load_session("coe-x:someone")
    assert ([m.text() for m in loaded], ver) == (["第一版"], 1)


@pytest.mark.integration
async def test_writing_from_a_stale_version_is_refused_and_writes_nothing(transcript_db):
    """别人在中间写过：抛出来，而不是把别人刚写下的那一整段盖掉。"""
    from app.data.session import get_session

    async with get_session() as s:
        await commit_transcript("coe-x:someone", [_said("别人写的")], expected_ver=0, session=s)

    with pytest.raises(TranscriptConflict):
        async with get_session() as s:
            await commit_transcript(
                "coe-x:someone", [_said("我这一版")], expected_ver=0, session=s
            )

    loaded, ver = await load_session("coe-x:someone")
    assert ([m.text() for m in loaded], ver) == (["别人写的"], 1)


@pytest.mark.integration
async def test_the_write_rides_the_callers_transaction(transcript_db):
    """调用方的事务回滚，这次写入也跟着没了 —— 它跟调用方的其他写入一起提交或一起不提交。"""
    from app.data.session import get_session

    with pytest.raises(RuntimeError, match="调用方后面出错"):
        async with get_session() as s:
            await commit_transcript(
                "coe-x:someone", [_said("没提交的一版")], expected_ver=0, session=s
            )
            raise RuntimeError("调用方后面出错")

    assert await load_session("coe-x:someone") == ([], 0)


# ---------------------------------------------------------------------------
# 四 · 阈值和素材表由调用方给
# ---------------------------------------------------------------------------


def test_the_layer_gives_no_default_policy_and_reads_no_config():
    """给了默认值的话，下一个调用方一个阈值都不写、一个工具都不分类也照跑，跑的是谁设计
    的那一套没人说得清，而且一句报错都没有。"""
    import app.agent.continuity as mod

    for fn, names in (
        (mod.trim_for_round, ("policy", "material_tools", "state", "now")),
        (mod.next_transcript, ("policy",)),
    ):
        params = inspect.signature(fn).parameters
        for name in names:
            assert params[name].default is inspect.Parameter.empty, (
                f"{fn.__name__} 的 {name} 有默认值"
            )
    assert all(
        f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
        for f in dataclasses.fields(TrimPolicy)
    ), "TrimPolicy 自带了默认阈值"
    for gone in ("dynamic_config", "DEFAULT_TRIM_POLICY", "load_trim_policy"):
        assert not hasattr(mod, gone), f"{gone} 在基础层里"


# ---------------------------------------------------------------------------
# 五 · 裁剪规则，对任意工具名
# ---------------------------------------------------------------------------


def test_a_material_payload_fades_but_its_call_stays():
    """过了素材那道线：载荷换成一句写死的短语，调用和结果这两条消息都还在。"""
    ctx = _play(
        start=_at(13, 0),
        until=_at(15, 0),
        events={"13:30": [_call("fetch_page", "c1"), _result("c1", "页面正文"), _said("嗯")]},
    )

    assert _tool_payload(ctx, "c1") == MATERIAL_TRIMMED
    assert any(tc.id == "c1" for m in ctx for tc in m.tool_calls)


def test_what_the_caller_did_not_call_material_keeps_its_payload():
    """素材表是调用方给的：不在表里的工具，同样的时刻载荷原样留着。"""
    ctx = _play(
        start=_at(13, 0),
        until=_at(15, 0),
        events={"13:30": [_call("note_down", "c1"), _result("c1", "记下了"), _said("嗯")]},
    )

    assert _tool_payload(ctx, "c1") == "记下了"


def test_a_whole_group_goes_past_the_own_window_and_leaves_no_orphan():
    ctx = _play(
        start=_at(13, 0),
        until=_at(18, 0),
        events={"13:30": [_call("fetch_page", "c1"), _result("c1", "页面正文"), _said("嗯")]},
    )

    assert not any(tc.id == "c1" for m in ctx for tc in m.tool_calls)
    assert all(m.tool_call_id != "c1" for m in ctx)
    assert _orphans(ctx) == []


def test_a_picture_only_lives_in_the_newest_generation():
    """它那一代之后插入第一条标记消息时，图片块就换成短语；同一条里的文字照留。"""
    picture = _result(
        "c1",
        [
            ContentBlock.from_text("pic=abc123"),
            ContentBlock.from_image_url({"url": "https://tos.example/signed?expires=1"}),
        ],
    )
    ctx = _play(
        start=_at(13, 30),
        until=_at(14, 0),
        events={"13:40": [_call("show_picture", "c1"), picture, _said("看到了")]},
    )

    blocks = next(m for m in ctx if m.tool_call_id == "c1").content
    assert [b.type for b in blocks] == ["text", "text"]
    assert blocks[0].text == "pic=abc123"
    assert blocks[1].text == PICTURE_TRIMMED


def test_a_lost_round_gets_a_gap_marker_and_keeps_the_history():
    """上一轮没存下来：没跨清理点也补一条缺口标记消息，时刻是这一轮，往前的历史一条不丢。"""
    history = _play(start=_at(13, 0), until=_at(13, 20), events={})

    fed = trim_for_round(
        history,
        now=_at(13, 40),
        state=STATE,
        policy=POLICY,
        material_tools=MATERIAL,
        lost_last_round=True,
    )

    assert fed[:-1] == history
    assert _marker_time(fed[-1], GAP_HEAD) == _at(13, 40)
    assert STATE in fed[-1].text()


def test_the_hard_cap_drops_the_oldest_groups_and_never_this_round():
    tight = dataclasses.replace(POLICY, hard_cap_tokens=2_000, trim_target_tokens=1_000)
    history = [_said(f"第{i}段，" + "长" * 300) for i in range(10)]
    this_round = [Message(role=Role.USER, content="现在 13:00。"), _said("这一轮")]

    stored = next_transcript(history, this_round, policy=tight)

    assert stored[-2:] == this_round
    assert "第0段" not in "".join(m.text() for m in stored)
    assert estimate_tokens(stored) <= tight.trim_target_tokens
