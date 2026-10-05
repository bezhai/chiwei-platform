"""分层裁剪 —— 她的上下文在固定时刻按两档时长裁，全程不做总结。

这个文件从她这一侧钉裁剪（机制在基础层 :mod:`app.agent.continuity`，她的阈值和
分类表在 :mod:`app.living.moment`）：

  1. **分类表覆盖全部工具**：加一只手不分类就跑不过，不会静默落进某一档；
  2. **时间语义按明确截止线验**：整点清理下"保留 1 小时"实际是 1–2 小时，用例
     写死具体时刻，不用"跑了很久没报错"当证据；
  3. **以一次完整的工具调用为单位**：调用还在就只换掉过期的载荷，整组过期才整组删，
     任何时候都不留没有结果的调用；
  4. **图片块比文本先走**：地址是 1.5 小时就死的预签名 URL，回放时 adapter 会重新
     下载，过期就是整轮抛错；
  5. **硬顶兜底会说话**：撞上必须留下一行日志，而且这一轮的东西不许被裁掉。
"""

# ruff: noqa: F811 — 用例的形参名就是从 test_moment 借过来的 fixture 名
from __future__ import annotations

import base64
import datetime as dt
import logging
from dataclasses import replace

import pytest

from app.agent.continuity import (
    CHECKPOINT_HEAD,
    MATERIAL_TRIMMED,
    PICTURE_TRIMMED,
    TrimPolicy,
    estimate_tokens,
    next_transcript,
    trim_for_round,
)
from app.agent.neutral import ContentBlock, Message, Role, ToolCall, TurnPart
from app.agent.session import load_session
from app.living.moment import (
    KEPT_TOOLS,
    MATERIAL_TOOLS,
    MOMENT_TOOLS,
    MOMENT_TRIM_POLICY,
    run_moment,
    transcript_key,
)
from tests.living.conftest import clock_at
from tests.living.test_moment import moment_db, stub_moment  # noqa: F401

LANE = "coe-living"
_CST = dt.timezone(dt.timedelta(hours=8))

# 一张 1x1 的 png，data: URI —— adapter 本地解码，不走网络。
_PNG = (
    "data:image/png;base64,"
    + base64.b64encode(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        )
    ).decode()
)

# 阈值搬到业务层**之前**线上跑的那一套。那五个 Dynamic Config key 线上一条都没配
# （2026-09-29 查管理接口全量，跨全部泳道），所以当时每一轮拿到的就是这五个数。
#
# 本文件的默认策略就用它，等价性也以它为基准：搬完之后两侧各自那份跑出来的结果，
# 必须跟拿这一份跑出来的逐条相同（见第七节）。
BEFORE_THE_MOVE = TrimPolicy(
    material_minutes=60,
    own_minutes=240,
    cleanup_minutes=60,
    hard_cap_tokens=200_000,
    trim_target_tokens=100_000,
)


def _at(hour: int, minute: int = 0, day: int = 25) -> dt.datetime:
    return dt.datetime(2026, 7, day, hour, minute, tzinfo=_CST)


def _stim(text: str) -> Message:
    return Message(role=Role.USER, content=text)


def _call(name: str, call_id: str, **args) -> Message:
    return Message(
        role=Role.ASSISTANT,
        content="",
        tool_calls=[ToolCall(id=call_id, name=name, arguments=args)],
    )


def _calls(*pairs: tuple[str, str]) -> Message:
    """一轮里同时发出好几个调用（provider 要求结果逐个对上）。"""
    return Message(
        role=Role.ASSISTANT,
        content="",
        tool_calls=[
            ToolCall(id=call_id, name=name, arguments={})
            for name, call_id in pairs
        ],
    )


def _result(call_id: str, content) -> Message:
    return Message(role=Role.TOOL, content=content, tool_call_id=call_id)


def _said(text: str) -> Message:
    return Message(role=Role.ASSISTANT, content=text)


def _round(
    history: list[Message],
    at: dt.datetime,
    *produced: Message,
    state: str = "手上：你在家/客厅，正在发呆。",
    policy: TrimPolicy = BEFORE_THE_MOVE,
    material_tools: frozenset[str] = MATERIAL_TOOLS,
) -> list[Message]:
    """跑一轮：先按这一刻裁一遍历史，再把这一轮的输入和产出接上去。

    两步的顺序跟 :func:`app.living.moment.run_moment` 一样 —— 裁在模型调用之前，
    存下去的就是喂进去的那份加上这一轮。
    """
    kept = trim_for_round(
        history, now=at, state=state, policy=policy, material_tools=material_tools
    )
    return next_transcript(
        kept, [_stim(f"现在 {at:%H:%M}。"), *produced], policy=policy
    )


def _play(
    *,
    start: dt.datetime,
    until: dt.datetime,
    events: dict[str, list[Message]] | None = None,
    policy: TrimPolicy = BEFORE_THE_MOVE,
    material_tools: frozenset[str] = MATERIAL_TOOLS,
    step: int = 10,
) -> list[Message]:
    """照她真实的节奏一轮一轮跑过去：默认每 10 分钟一个「继续」。

    ``events`` 按 ``"13:30"`` 这样的钟点挂这一轮的产出。用真实节奏而不是"隔两小时跑
    一轮"，是因为界桩每个清理周期才立一根：跳着跑测出来的截止线是另一条线。
    """
    ctx: list[Message] = []
    at = start
    while at <= until:
        produced = (events or {}).get(f"{at:%H:%M}") or [_said("继续")]
        ctx = _round(
            ctx, at, *produced, policy=policy, material_tools=material_tools
        )
        at += dt.timedelta(minutes=step)
    return ctx


def _texts(messages: list[Message]) -> list[str]:
    return [m.text() for m in messages]


def _images(messages: list[Message]) -> list[ContentBlock]:
    return [
        b
        for m in messages
        if isinstance(m.content, list)
        for b in m.content
        if b.type in ("image", "image_url")
    ]


# ---------------------------------------------------------------------------
# 一 · 长期标识清单：每只手都得分类
# ---------------------------------------------------------------------------


def test_every_tool_she_has_is_classified():
    """她手上 20 只手，每一只都要明确落进"素材"或"留着"其中一档。

    漏分类的那只会落进默认档，而默认档是哪一档取决于实现细节 —— 一只带图片句柄的
    新工具静默落进"素材"，就是她拿着一个失效的句柄去发图。
    """
    every = {t.name for t in MOMENT_TOOLS}
    assert MATERIAL_TOOLS | KEPT_TOOLS == every, (
        f"没分类的：{every - (MATERIAL_TOOLS | KEPT_TOOLS)}；"
        f"分类表里多出来的：{(MATERIAL_TOOLS | KEPT_TOOLS) - every}"
    )
    assert not (MATERIAL_TOOLS & KEPT_TOOLS)


def test_the_tools_that_hand_her_a_handle_are_all_kept():
    """返回里带句柄的那几只，一只都不能在素材那一档。

    ``pic=`` / ``file=`` / ``channel_id=`` 是她后面调别的工具要原样抄回去的凭据。
    """
    for name in (
        "draw_a_picture",
        "find_a_picture_online",
        "look_through_your_pictures",
        "look_at_a_picture",
        "look_for_something_to_read",
        "read_a_bit",
        "look_up_contact",
        "send_message",
        "take_back_message",
        # 这一页上两样：每条她自己发的消息带的 ``take_back_id``，和头上那串
        # ``before=``（往前翻唯一的入口）。
        "look_at_phone",
    ):
        assert name in KEPT_TOOLS, f"{name} 的返回里有她后面还要用的东西"


# ---------------------------------------------------------------------------
# 二 · 时间语义：整点清理，"保留 1 小时"实际是 1–2 小时
# ---------------------------------------------------------------------------


def test_two_rounds_in_the_same_hour_leave_the_prefix_untouched():
    """同一个清理周期里，前缀一个字节都不动 —— 前缀缓存才有得命中。"""
    first = _round([], _at(14, 3), _said("继续"))
    second = _round(first, _at(14, 13), _said("继续"))
    third = _round(second, _at(14, 23), _said("继续"))

    assert [m.to_replay_dict() for m in second[: len(first)]] == [
        m.to_replay_dict() for m in first
    ]
    assert [m.to_replay_dict() for m in third[: len(second)]] == [
        m.to_replay_dict() for m in second
    ]


def test_material_from_just_before_a_cleanup_lives_into_the_next_hour():
    """13:50 读到的网页，14:50 还在眼前，15:00 那次清理才走 —— 截止线是明确的。

    整点清理下"保留 1 小时"实际是 1–2 小时：这一条把两头都钉住。
    """
    page = "搜到这些：抹茶店周一休息"
    read_it = [
        _call("search_online", "c1"),
        _result("c1", page),
        _said("哦"),
    ]

    assert page in "".join(
        _texts(_play(start=_at(13, 0), until=_at(14, 50), events={"13:50": read_it}))
    ), "还没跨过下一个清理点"

    assert page not in "".join(
        _texts(_play(start=_at(13, 0), until=_at(15, 0), events={"13:50": read_it}))
    ), "跨过 15:00 这个清理点就该没了"


def test_the_expired_payload_becomes_one_fixed_phrase_not_a_summary():
    """过期的载荷换成一句代码写死的短语，不是概括 —— 总结会留下一个可能已经错了的版本。"""
    ctx = _play(
        start=_at(13, 0),
        until=_at(15, 0),
        events={
            "13:30": [
                _call("search_online", "c1"),
                _result("c1", "搜到这些：抹茶店周一休息、隔壁那家周三休息"),
                _said("知道了"),
            ]
        },
    )

    trimmed = [m for m in ctx if m.role is Role.TOOL and m.tool_call_id == "c1"]
    assert len(trimmed) == 1, "调用还在保留期内，它的结果这条消息就必须还在"
    assert trimmed[0].text() == MATERIAL_TRIMMED


def test_her_own_words_go_at_the_four_hour_line():
    """她自己的话保留 4 小时；素材那一档早就换掉了，她说的还在。"""
    said = {"10:30": [_said("我去洗澡了")]}

    assert "我去洗澡了" in _texts(
        _play(start=_at(10, 0), until=_at(14, 0), events=said)
    ), "还不到 4 小时"

    assert "我去洗澡了" not in _texts(
        _play(start=_at(10, 0), until=_at(15, 0), events=said)
    ), "过了 4 小时该没了"


# ---------------------------------------------------------------------------
# 三 · 以一次完整的工具调用为单位
# ---------------------------------------------------------------------------


def _orphans(messages: list[Message]) -> list[str]:
    """没有结果的调用 id —— provider 会因为它拒掉整个请求。"""
    answered = {m.tool_call_id for m in messages if m.role is Role.TOOL}
    return [
        tc.id
        for m in messages
        for tc in m.tool_calls
        if tc.id not in answered
    ]


def test_a_call_group_goes_as_one_piece():
    """整组过期就整组删，任何时候都不留一个没有结果的调用。"""
    searched = {
        "10:30": [
            _call("search_online", "c1"),
            _result("c1", "网页正文"),
            _said("哦"),
        ]
    }

    ctx = _play(start=_at(10, 0), until=_at(13, 0), events=searched)
    assert _orphans(ctx) == []
    assert any(m.tool_calls for m in ctx), "还没到 4 小时，调用本身还在"

    ctx = _play(start=_at(10, 0), until=_at(15, 0), events=searched)
    assert _orphans(ctx) == []
    assert not any(m.tool_calls for m in ctx), "整组过期了，调用和结果一起走"


def test_several_calls_in_one_turn_keep_their_results_paired():
    """同一轮里发出去三个调用，裁完还是三个调用三个结果，而且一一对上。"""
    ctx = _play(
        start=_at(13, 0),
        until=_at(15, 0),
        events={
            "13:30": [
                _calls(
                    ("search_online", "c1"),
                    ("look_around", "c2"),
                    ("draw_a_picture", "c3"),
                ),
                _result("c1", "网页正文"),
                _result("c2", "你在客厅，这里没别人。"),
                _result("c3", "你画出来了：「晚霞」 pic=abc123"),
                _said("画完了"),
            ]
        },
    )

    calls = [tc.id for m in ctx for tc in m.tool_calls]
    results = [m.tool_call_id for m in ctx if m.role is Role.TOOL]
    assert calls == ["c1", "c2", "c3"]
    assert results == ["c1", "c2", "c3"]
    # 素材那两只换成短语，带句柄那只原样留着
    by_id = {m.tool_call_id: m.text() for m in ctx if m.role is Role.TOOL}
    assert by_id["c1"] == MATERIAL_TRIMMED
    assert by_id["c2"] == MATERIAL_TRIMMED
    assert "pic=abc123" in by_id["c3"]


def test_a_handle_outlives_the_material_window():
    """句柄那一档跟着她自己的话走 4 小时，不是 1 小时。"""
    listed = {
        "13:30": [
            _call("look_through_your_pictures", "c1"),
            _result("c1", "你手上的图：\n- 「晚霞」 pic=abc123"),
            _said("找到了"),
        ]
    }

    assert "pic=abc123" in "".join(
        _texts(_play(start=_at(13, 0), until=_at(16, 0), events=listed))
    ), "3 小时后她还得指得出那张图"

    assert "pic=abc123" not in "".join(
        _texts(_play(start=_at(13, 0), until=_at(18, 0), events=listed))
    )


def test_the_phone_page_keeps_its_handles_past_the_material_window():
    """「看手机」那一页带的两串凭据跟着她自己的话走 4 小时，不是 1 小时。

    ``take_back_id`` 在状态快照"你刚做过、说过"那段确实有副本，但那段只有最近 12
    条 —— 滚出去的旧消息就没有第二份了；``before=`` 更是从头到尾只出现在这一次返回
    里，换掉整段载荷之后她就再也翻不回这条会话更早的地方。
    """
    looked = {
        "13:30": [
            _call("look_at_phone", "c1", channel_id="g1"),
            _result(
                "c1",
                "「宅居研究所」（其中 1 条是新的；前面还有 30 条，想往前翻就带上 "
                "before=b3ad23ff-0e3e-443a-8fcb-e2a6c73169bf）\n"
                '<msg from="你" time="13:29 CST" take_back_id="deadbeef">我在</msg>',
            ),
            _said("看完了"),
        ]
    }

    joined = "".join(
        _texts(_play(start=_at(13, 0), until=_at(16, 0), events=looked))
    )
    assert 'take_back_id="deadbeef"' in joined, "3 小时后她撤不回自己刚说的那句了"
    assert "before=b3ad23ff-0e3e-443a-8fcb-e2a6c73169bf" in joined, (
        "往前翻那串只有这一次返回里有，换掉就再也翻不回去了"
    )


# ---------------------------------------------------------------------------
# 四 · 图片块：比文本先走，因为地址会死
# ---------------------------------------------------------------------------


def test_pictures_leave_at_the_first_cleanup_while_the_handle_stays():
    """图片块在下一个清理点就不在了，同一条返回里的 ``pic=`` 留着。

    地址是预签名 URL，90 分钟就死。留过头的后果不是"她看不清"，是回放时 adapter
    重新下载拿到 403、整轮在模型请求之前抛错。
    """
    drew = {
        "14:30": [
            _call("draw_a_picture", "c1"),
            _result(
                "c1",
                [
                    ContentBlock.from_text("你画出来了：「晚霞」 pic=abc123"),
                    ContentBlock.from_image_url({"url": _PNG}),
                ],
            ),
            _said("画完了"),
        ]
    }

    ctx = _play(start=_at(14, 0), until=_at(14, 50), events=drew)
    assert len(_images(ctx)) == 1, "同一个清理周期内不动"

    ctx = _play(start=_at(14, 0), until=_at(15, 0), events=drew)
    assert _images(ctx) == [], "跨过清理点，图片块必须走"
    joined = "".join(_texts(ctx))
    assert "pic=abc123" in joined, "句柄是她以后找回这张图的凭据"
    assert PICTURE_TRIMMED in joined


def test_a_picture_never_outlives_its_signed_url():
    """清理周期配到上限时，一张图仍然活不过地址的签名寿命。

    最坏情况是图片刚过一个清理点就进来：它要等下下个清理点才走，也就是一个周期加
    一个 moment 间隔。这一条钉的是这两个数加起来仍然小于签名寿命 —— 谁把周期往上调，
    这里先炸，而不是上线以后每一轮都在同一个地方抛错。
    """
    from app.agent.continuity import MAX_CLEANUP_MINUTES, PICTURE_URL_MINUTES
    from app.living.moment import DEFAULT_LIFE_MOMENT_MINUTES

    assert MAX_CLEANUP_MINUTES + DEFAULT_LIFE_MOMENT_MINUTES < PICTURE_URL_MINUTES

    slow = TrimPolicy(
        material_minutes=60,
        own_minutes=240,
        cleanup_minutes=MAX_CLEANUP_MINUTES,
        hard_cap_tokens=200_000,
        trim_target_tokens=100_000,
    )
    drew = {
        "14:10": [
            _call("look_at_a_picture", "c1"),
            _result(
                "c1",
                [
                    ContentBlock.from_text("「晚霞」 pic=abc123"),
                    ContentBlock.from_image_url({"url": _PNG}),
                ],
            ),
        ]
    }
    gone_by = _at(14, 10) + dt.timedelta(
        minutes=MAX_CLEANUP_MINUTES + DEFAULT_LIFE_MOMENT_MINUTES
    )
    ctx = _play(start=_at(13, 0), until=gone_by, events=drew, policy=slow)
    assert _images(ctx) == []


async def test_the_trimmed_transcript_never_asks_for_a_dead_url(monkeypatch):
    """裁完的上下文交给真 adapter 编码，不会去下载任何过期地址。

    把下载打成 403：只要裁剪确实把图片块拿掉了，这一步根本不会被调到，编码照常完成。
    """
    from app.agent.adapters import gemini as gemini_mod

    async def forbidden(_url):
        raise RuntimeError("403 Forbidden：预签名地址过期了")

    monkeypatch.setattr(gemini_mod, "_fetch_remote_image", forbidden)

    shown = [
        ContentBlock.from_text("「晚霞」 pic=abc123"),
        ContentBlock.from_image_url({"url": "https://tos.example/signed?expires=1"}),
    ]
    ctx = _round(
        [], _at(13, 5), _call("look_at_a_picture", "c1"), _result("c1", shown), _said("好看")
    )
    ctx = _round(ctx, _at(15, 5), _said("继续"))

    adapter = gemini_mod.GeminiAdapter(model_name="gemini-2.5", api_key="k", base_url=None)
    contents, _system = await adapter._to_wire_contents(ctx)
    assert contents, "编码本身必须走得通"


async def test_the_trimmed_transcript_encodes_for_gemini():
    """裁完的消息序列过真 adapter：每个 model 轮的调用数和结果部件数对得上。

    Gemini 的硬要求是"回答一个 model 轮的 function_response 部件数必须等于那一轮的
    function_call 部件数"，不是"开头没有孤儿"就行。
    """
    from app.agent.adapters.gemini import GeminiAdapter

    ctx = _round(
        [],
        _at(13, 5),
        _calls(("search_online", "c1"), ("look_at_a_picture", "c2")),
        _result("c1", "网页正文"),
        _result(
            "c2",
            [
                ContentBlock.from_text("「晚霞」 pic=abc123"),
                ContentBlock.from_image_url({"url": _PNG}),
            ],
        ),
        _said("看完了"),
    )
    ctx = _round(ctx, _at(14, 5), _call("say", "c3"), _result("c3", "记下了：嗯"), _said("嗯"))
    ctx = _round(ctx, _at(15, 5), _said("继续"))

    adapter = GeminiAdapter(model_name="gemini-2.5", api_key="k", base_url=None)
    contents, _system = await adapter._to_wire_contents(ctx)

    pending = 0
    for content in contents:
        calls = sum(
            1 for p in (content.parts or []) if getattr(p, "function_call", None)
        )
        answers = sum(
            1 for p in (content.parts or []) if getattr(p, "function_response", None)
        )
        if calls:
            assert pending == 0, "上一轮的调用还没被回答完"
            pending = calls
        elif answers:
            assert answers == pending, "回答数必须等于那一轮的调用数"
            pending = 0
    assert pending == 0, "最后还有调用没被回答"


# ---------------------------------------------------------------------------
# 五 · 清理时重铺一次状态
# ---------------------------------------------------------------------------


def test_a_cleanup_lays_her_state_down_as_the_new_starting_point():
    """清理那一下把她此刻的状态插进去，作为往后那一段的起点。"""
    ctx = _round([], _at(13, 5), _said("继续"))
    ctx = _round(ctx, _at(14, 5), _said("继续"), state="手上：你在家/浴室，正在洗澡。")

    joined = "\n".join(_texts(ctx))
    assert CHECKPOINT_HEAD in joined
    assert "手上：你在家/浴室，正在洗澡。" in joined
    # 状态落在这一轮的输入之前 —— 它是新起点，不是这一轮发生的事
    state_at = next(i for i, m in enumerate(ctx) if "正在洗澡" in m.text())
    stim_at = next(i for i, m in enumerate(ctx) if m.text() == "现在 14:05。")
    assert state_at < stim_at


def test_no_state_is_laid_down_between_two_cleanups():
    """两次清理之间一个字都不加 —— 加了前缀就变了。"""
    ctx = _round([], _at(14, 5), _said("继续"))
    ctx = _round(ctx, _at(14, 15), _said("继续"))
    ctx = _round(ctx, _at(14, 35), _said("继续"), state="不该出现的状态")
    assert "不该出现的状态" not in "\n".join(_texts(ctx))
    assert sum(1 for m in ctx if CHECKPOINT_HEAD in m.text()) == 1


def test_the_first_round_of_a_day_starts_from_her_state():
    """一天的第一轮历史是空的 —— 那一下也要立一根界桩。

    每轮的刺激只送新发生的事（:func:`app.living.moment.run_moment`），全量状态只从界桩
    来。历史空的时候不立，她这一轮就不知道自己在哪、在做什么、心里挂着什么 —— 跨过
    04:00 的第一轮、以及重启之后的第一轮，都是这个形状。
    """
    ctx = _round([], _at(9, 5), _said("继续"), state="手上：你在家/浴室，正在洗澡。")

    assert len(ctx) == 3
    assert CHECKPOINT_HEAD in ctx[0].text()
    assert "手上：你在家/浴室，正在洗澡。" in ctx[0].text()
    assert _texts(ctx[1:]) == ["现在 09:05。", "继续"]


# ---------------------------------------------------------------------------
# 六 · 硬顶兜底
# ---------------------------------------------------------------------------


def _tiny_cap(cap: int, target: int) -> TrimPolicy:
    return TrimPolicy(
        material_minutes=60,
        own_minutes=240,
        cleanup_minutes=60,
        hard_cap_tokens=cap,
        trim_target_tokens=target,
    )


def test_the_hard_cap_trims_to_the_target_and_leaves_a_line_in_the_log(caplog):
    """撞上硬顶就裁到目标值，而且绝不静默。"""
    policy = _tiny_cap(cap=600, target=300)
    ctx: list[Message] = []
    with caplog.at_level(logging.WARNING, logger="app.agent.continuity"):
        for i in range(12):
            ctx = _round(
                ctx,
                _at(14, i),
                _said("她这一轮说的话，" + "凑字数" * 40),
                policy=policy,
            )

    assert estimate_tokens(ctx) <= policy.hard_cap_tokens
    assert any("硬顶" in r.message for r in caplog.records), caplog.text


def test_the_hard_cap_never_eats_this_round(caplog):
    """哪怕这一轮自己就超了，这一轮的东西也必须原样留下来。"""
    policy = _tiny_cap(cap=50, target=20)
    huge = _said("这一轮她说了很长一段，" + "字" * 2000)
    with caplog.at_level(logging.ERROR, logger="app.agent.continuity"):
        ctx = _round([], _at(14, 5), huge, policy=policy)

    assert huge.text() in _texts(ctx)
    assert any("硬顶" in r.message for r in caplog.records), caplog.text


def test_a_round_that_blew_past_the_cap_is_cut_back_before_the_next_model_call():
    """一轮自己就撑爆硬顶，下一轮**喂给模型之前**必须先裁回顶以下。

    上面那条（"哪怕这一轮自己就超了也原样留下来"）是对的：丢掉刚发生的事等于这一轮
    白跑。但那一份紧接着就是下一轮的历史，而它大到会让模型请求当场失败 —— 失败就不
    提交，下一轮读到同一份，再立一根**时刻是当下**的界桩，那一代的年龄永远是 0，两档
    时长一条都够不着。日界清零没了之后这是个谁也走不出来的循环。

    所以硬顶在模型调用之前也要判一次。判在这里恢复才不依赖"下一轮得先成功提交一次"。
    """
    policy = _tiny_cap(cap=600, target=300)
    blown = _round([], _at(14), _said("这一轮她说了很长一段，" + "字" * 2000), policy=policy)
    assert estimate_tokens(blown) > policy.hard_cap_tokens, (
        "前提没成立：这一份根本没撑爆硬顶，这条用例什么都没验"
    )

    fed = trim_for_round(
        blown,
        now=_at(16),
        state="手上：你在家/客厅，正在发呆。",
        policy=policy,
        material_tools=MATERIAL_TOOLS,
    )

    assert estimate_tokens(fed) <= policy.hard_cap_tokens, (
        f"喂给模型的那份还是 {estimate_tokens(fed)} token —— 这一拍会在模型那步炸掉，"
        f"炸了就不提交，下一拍原样再来一次"
    )
    assert any(CHECKPOINT_HEAD in t for t in _texts(fed)), (
        "连这一轮刚立的界桩都被兜底裁掉了 —— 那她眼前一句「你现在」都没有"
    )


def test_the_hard_cap_drops_whole_groups():
    """兜底也按组裁，裁完不许留没有结果的调用。"""
    policy = _tiny_cap(cap=400, target=200)
    ctx: list[Message] = []
    for i in range(10):
        ctx = _round(
            ctx,
            _at(14, i),
            _call("search_online", f"c{i}"),
            _result(f"c{i}", "网页正文，" + "很长" * 60),
            _said("看完了"),
            policy=policy,
        )
    assert _orphans(ctx) == []


def test_the_token_estimate_errs_high():
    """没有现成的 tokenizer，估算一律往高了估 —— 估低了才会真的撞上模型的上限。"""
    cjk = Message(role=Role.USER, content="中" * 1000)
    assert estimate_tokens([cjk]) >= 1000

    ascii_only = Message(role=Role.USER, content="a" * 1000)
    assert estimate_tokens([ascii_only]) >= 250

    with_picture = Message(
        role=Role.TOOL,
        content=[
            ContentBlock.from_text("一张图"),
            ContentBlock.from_image_url({"url": _PNG}),
        ],
        tool_call_id="c1",
    )
    text_only = Message(role=Role.TOOL, content="一张图", tool_call_id="c1")
    assert estimate_tokens([with_picture]) > estimate_tokens([text_only]) + 250


def test_what_she_thought_counts_toward_the_estimate():
    """思考正文占的是同一份 context —— 不算进去就低估了上下文体积。"""
    spoken = Message.from_model_turn([TurnPart.from_text("嗯")], [])
    with_thinking = Message.from_model_turn(
        [TurnPart.from_thought("想" * 300, signature=b"sig"), TurnPart.from_text("嗯")],
        [],
    )
    assert estimate_tokens([with_thinking]) >= estimate_tokens([spoken]) + 300


# ---------------------------------------------------------------------------
# 七 · 她那份策略写在她自己的模块里
#
# 基础层（:mod:`app.agent.continuity`）只给形状和裁剪动作，她那份阈值写死在 moment
# 模块里。基础层自己不留阈值、不给默认值那一条在 ``tests/agent/test_continuity.py``。
# 这一节验三样：
#
#   1. **等价**：阈值搬到业务层之后，同一份历史、同一时刻、同一状态下，喂进模型的那一份
#      和落盘的那一份跟搬之前逐条相同。四条边界各走一遍。值本身另钉一条，因为整点清理
#      下裁剪结果对分钟级的差别不敏感；
#   2. **读的是自己那个名字**：她这一轮裁剪和收尾各一次，两次读的都是 moment 模块里那份；
#   3. **约束**：原来那个运行时校验函数表达的五条约束，原样钉在这份常量上。
# ---------------------------------------------------------------------------

# 拿来验素材那一档的那只手，必须在她自己的素材表里。
_MATERIAL_TOOL = "search_online"


def _replay_both(
    *,
    start: dt.datetime,
    until: dt.datetime,
    events: dict[str, list[Message]] | None = None,
    policy: TrimPolicy,
    material_tools: frozenset[str],
    step: int = 10,
) -> list[tuple[list[Message], list[Message]]]:
    """照真实节奏跑过去，每一轮**喂进模型的那份**和**落盘的那份**都收下来。

    :func:`_play` 只交回最后落盘的那一份，而等价承诺覆盖的是两份 —— 喂进去的那份
    要是变了，她眼前的东西就变了，哪怕存下来的最终一样。
    """
    rounds: list[tuple[list[Message], list[Message]]] = []
    ctx: list[Message] = []
    at = start
    while at <= until:
        produced = (events or {}).get(f"{at:%H:%M}") or [_said("继续")]
        fed = trim_for_round(
            ctx,
            now=at,
            state="手上：你在家/客厅，正在发呆。",
            policy=policy,
            material_tools=material_tools,
        )
        stored = next_transcript(
            fed, [_stim(f"现在 {at:%H:%M}。"), *produced], policy=policy
        )
        rounds.append((fed, stored))
        ctx = stored
        at += dt.timedelta(minutes=step)
    return rounds


def _verbatim(rounds: list[tuple[list[Message], list[Message]]]) -> list[list[dict]]:
    """逐条比对用的形状：``to_replay_dict`` 是无损的，签名也在里面。"""
    return [[m.to_replay_dict() for m in half] for both in rounds for half in both]


def _boundary_rounds(
    boundary: str, *, policy: TrimPolicy
) -> list[tuple[list[Message], list[Message]]]:
    """四条边界各自的那段历史。跑法只由 ``policy`` 决定，两次调用只换它。"""
    read_it = [
        _call(_MATERIAL_TOOL, "c1"),
        _result("c1", "读到的那一段：抹茶店周一休息"),
        _said("知道了"),
    ]
    plans = {
        # 跨过素材那道线、没跨整组那道：载荷换成短语，调用和结果都还在
        "素材折叠": {
            "start": _at(13, 0),
            "until": _at(15, 0),
            "events": {"13:30": read_it},
        },
        # 跨过整组那道线：调用和它的全部结果一起走
        "整组丢弃": {
            "start": _at(13, 0),
            "until": _at(18, 0),
            "events": {"13:30": read_it},
        },
        # 跨过一个清理点：界桩插进去，往后那一段从它起算
        "界桩插入": {
            "start": _at(13, 50),
            "until": _at(14, 10),
            "events": None,
        },
        # 撑爆硬顶：线上离它很远，只能在这儿造出来。每段写死一个不同的开头，
        # 兜底把最老那一段丢掉这件事才有得断言。
        "token 硬顶": {
            "start": _at(14, 0),
            "until": _at(14, 30),
            "events": {
                "14:00": [_said("第一段，" + "很长" * 35_000)],
                "14:10": [_said("第二段，" + "很长" * 35_000)],
                "14:20": [_said("第三段，" + "很长" * 35_000)],
                "14:30": [_said("第四段，" + "很长" * 35_000)],
            },
        },
    }
    return _replay_both(
        **plans[boundary], policy=policy, material_tools=MATERIAL_TOOLS
    )


def _assert_boundary_was_hit(
    boundary: str,
    rounds: list[tuple[list[Message], list[Message]]],
    *,
    policy: TrimPolicy,
) -> None:
    """这段历史真的走到那条边界上了 —— 不然下面那句"两份相同"什么都没验。"""
    stored = rounds[-1][1]
    joined = "".join(_texts(stored))
    if boundary == "素材折叠":
        assert MATERIAL_TRIMMED in joined, "载荷没被换成短语"
        assert any(m.tool_calls for m in stored), "调用本身也走了，那是整组丢弃"
    elif boundary == "整组丢弃":
        assert not any(m.tool_calls for m in stored), "调用还在，没走到整组那道线"
        assert MATERIAL_TRIMMED not in joined
        assert _orphans(stored) == []
    elif boundary == "界桩插入":
        assert sum(1 for t in _texts(stored) if CHECKPOINT_HEAD in t) == 2, (
            "该有两根：一根是空历史那一下，一根是跨 14:00 那一下"
        )
    elif boundary == "token 硬顶":
        assert "第一段" not in joined, "最老那一段还在，硬顶没兜住"
        assert estimate_tokens(stored) <= policy.hard_cap_tokens
    else:  # pragma: no cover - 参数写错时当场炸
        raise AssertionError(boundary)


@pytest.mark.parametrize(
    "boundary", ["素材折叠", "整组丢弃", "界桩插入", "token 硬顶"]
)
def test_moving_the_thresholds_changes_nothing_that_reaches_her(boundary):
    """搬完之后喂进去的那份和落盘的那份跟搬之前逐条相同，四条边界各走一遍。

    基准是 :data:`BEFORE_THE_MOVE`（搬之前线上每一轮实际拿到的那五个数）。先确认这段
    历史真的走到了那条边界上，再逐条比 —— 不然"两份相同"可能只是两边都什么也没裁。
    """
    before = _boundary_rounds(boundary, policy=BEFORE_THE_MOVE)
    _assert_boundary_was_hit(boundary, before, policy=BEFORE_THE_MOVE)

    after = _boundary_rounds(boundary, policy=MOMENT_TRIM_POLICY)
    assert _verbatim(after) == _verbatim(before)


def test_her_numbers_did_not_change_in_the_move():
    """她那五个数跟搬之前完全一致 —— 那一次只换位置，不调值。

    上面那几条比的是**裁剪结果**，而整点清理下结果对分钟级的差别不敏感：把
    ``own_minutes`` 从 240 改成 239，裁出来逐条一模一样。所以值本身在这里单独钉一遍。
    往后要调她的阈值，改的就是这一条。
    """
    assert MOMENT_TRIM_POLICY == BEFORE_THE_MOVE


# ---------------------------------------------------------------------------
# 她这一轮读的是她自己那个名字
# ---------------------------------------------------------------------------


def _spy_on_the_policy(monkeypatch, module) -> dict[str, list[TrimPolicy]]:
    """记下这个模块裁剪和收尾**各自**实际传进去的那份策略，按调用点分开收。

    **分开收是必须的。** 合成一张单子的话，"只有裁剪走到了、收尾没走到"跟"两处都
    走到了"长得一模一样（``all`` 对少了一项的单子照样成立），而收尾那次漏掉策略的
    症状是落盘的那一份按另一套裁 —— 喂进去的和存下来的从此不是同一份前缀。
    """
    used: dict[str, list[TrimPolicy]] = {"trim": [], "next": []}
    real_trim = module.trim_for_round
    real_next = module.next_transcript

    def trim(history, **kwargs):
        used["trim"].append(kwargs["policy"])
        return real_trim(history, **kwargs)

    def nxt(history, produced, **kwargs):
        used["next"].append(kwargs["policy"])
        return real_next(history, produced, **kwargs)

    monkeypatch.setattr(module, "trim_for_round", trim)
    monkeypatch.setattr(module, "next_transcript", nxt)
    return used


def _assert_read_it_at_both_call_sites(
    used: dict[str, list[TrimPolicy]], expected: TrimPolicy, *, whose: str
) -> None:
    """裁剪和收尾**各一次**，两次拿到的都是 ``expected`` 那个对象。

    ``is`` 而不是 ``==``：替身跟原件逐项相等，值比不出"读的到底是哪个名字"。
    """
    counted = {site: len(seen) for site, seen in used.items()}
    assert counted == {"trim": 1, "next": 1}, (
        f"{whose}这一轮裁剪和收尾该各走一次，实际是 {counted}"
    )
    assert used["trim"][0] is expected, f"{whose}裁剪那次读的不是它自己那份"
    assert used["next"][0] is expected, f"{whose}收尾那次读的不是它自己那份"


@pytest.mark.integration
async def test_her_round_trims_by_her_own_policy(
    moment_db, stub_moment, monkeypatch
):
    """她这一轮裁剪和收尾各一次，两次读的都是 **moment 模块里那个名字**。

    她那份换成一个同值但不同一的替身：断言的是换进去的那个对象，所以"她拿的是别处
    的一份"会红。
    """
    from app.living import moment as moment_mod

    hers = replace(MOMENT_TRIM_POLICY)  # 同值，不同对象
    monkeypatch.setattr(moment_mod, "MOMENT_TRIM_POLICY", hers)
    used = _spy_on_the_policy(monkeypatch, moment_mod)

    stub_moment(said="继续")
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(13, 50)))

    _assert_read_it_at_both_call_sites(used, hers, whose="她")


# ---------------------------------------------------------------------------
# 约束：原来那个运行时校验表达的五条，一条不增一条不减
# ---------------------------------------------------------------------------


def test_both_windows_are_positive():
    """一 · 素材档和整组档两个时长都是正数。"""
    policy = MOMENT_TRIM_POLICY
    assert policy.material_minutes > 0
    assert policy.own_minutes > 0


def test_the_own_window_is_not_shorter_than_the_material_one():
    """二 · 整组档**不小于**素材档 —— 不是严格大于，两个数相等是允许的。

    比素材短的话，载荷已经换掉的那一组会比调用先走，留下没有结果的调用。
    """
    policy = MOMENT_TRIM_POLICY
    assert policy.own_minutes >= policy.material_minutes


def test_the_cleanup_period_stays_inside_the_bound():
    """三 · 清理周期落在 1 到上限之间（上限是基建层那条从图片地址寿命派生的事实）。"""
    from app.agent.continuity import MAX_CLEANUP_MINUTES

    policy = MOMENT_TRIM_POLICY
    assert 0 < policy.cleanup_minutes <= MAX_CLEANUP_MINUTES


def test_the_cap_and_the_target_are_positive():
    """四 · 硬顶和裁剪目标都是正数。"""
    policy = MOMENT_TRIM_POLICY
    assert policy.hard_cap_tokens > 0
    assert policy.trim_target_tokens > 0


def test_the_target_is_strictly_below_the_cap():
    """五 · 裁剪目标**严格**小于硬顶，不然撞顶之后裁不下去。"""
    policy = MOMENT_TRIM_POLICY
    assert policy.trim_target_tokens < policy.hard_cap_tokens


# ---------------------------------------------------------------------------
# 八 · 真的跑一轮：裁剪落在存下来的那一份上
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_cleanup_lands_in_the_stored_transcript(moment_db, stub_moment):
    """真跑三个 moment 跨过一个清理点：存下来的那一份里状态重铺过、素材换成了短语。

    第二个 moment 立起第一根界桩，第三个才有得算年龄 —— 这就是一天头几轮的样子。
    """
    stub_moment(("look_around", {}), said="继续")
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(13, 50)))

    stub_moment(said="继续")
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14, 0)))

    stub_moment(said="继续")
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(15, 10)))

    tid = transcript_key(lane=LANE, persona_id="akao")
    stored, _ver = await load_session(tid)
    joined = "\n".join(_texts(stored))

    assert MATERIAL_TRIMMED in joined, "look_around 的返回是素材，跨过清理点该换掉"
    assert _orphans(stored) == []
    assert CHECKPOINT_HEAD in joined, "清理那一下要把她当时的状态重铺进去"


@pytest.mark.integration
async def test_a_picture_is_gone_before_she_is_fed_again(moment_db, stub_moment):
    """停机跨过签名寿命再起来：喂给模型的那份里已经没有图片块了。

    裁剪如果只发生在收尾，这条路永远走不到 —— adapter 会在模型请求之前拿 403 抛错，
    每一轮都炸在同一个地方，收尾轮不上。所以裁必须在喂之前。
    """
    from app.agent.continuity import commit_transcript
    from app.data.session import get_session

    # 先塞一段带图片的历史，模拟上一个进程 13:00 那一轮画过一张
    tid = transcript_key(lane=LANE, persona_id="akao")

    async with get_session() as s:
        await commit_transcript(
            tid,
            [
                _stim("现在 13:00。"),
                _call("draw_a_picture", "c1"),
                _result(
                    "c1",
                    [
                        ContentBlock.from_text("你画出来了：「晚霞」 pic=abc123"),
                        ContentBlock.from_image_url(
                            {"url": "https://tos.example/signed?expires=1"}
                        ),
                    ],
                ),
                _said("画完了"),
            ],
            expected_ver=0,
            session=s,
        )

    runner = stub_moment(said="继续")
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(15, 10)))

    fed = runner.runs[0][0]
    assert _images(fed) == [], "过期地址在模型看到它之前就该没了"
    assert "pic=abc123" in "".join(_texts(fed)), "句柄还得在"


# ---------------------------------------------------------------------------
# 九 · 这一轮的思考和签名在裁剪里的去留
#
# 一轮是一个整体：那一轮的调用带着签名，思考也带着签名，模型靠它们一起恢复这一轮的
# 推理。所以思考不单独设一档 —— 要么整轮留着（签名一个不少），要么整轮走（不留下
# 一段没有主的思考）。
# ---------------------------------------------------------------------------


def _thought_round(call_id: str, name: str, thought: str, sig: bytes) -> Message:
    call = ToolCall(id=call_id, name=name, arguments={}, signature=b"call-" + sig)
    return Message.from_model_turn(
        [
            TurnPart.from_thought(thought, signature=sig),
            TurnPart.from_tool_call(call),
        ],
        [call],
    )


def test_a_round_that_survives_keeps_its_signatures():
    """载荷换掉了，这一轮的思考和签名还在 —— 换的是工具返回，不是这一轮本身。"""
    ctx = _play(
        start=_at(13, 0),
        until=_at(15, 10),
        events={
            "13:10": [
                _thought_round("s1", "search_online", "查一下这个", b"sig-13"),
                _result("s1", "网页正文，很长很长"),
                _said("看完了"),
            ]
        },
    )

    turn = next(m for m in ctx if m.turn_parts)
    assert turn.thought_text() == "查一下这个"
    assert turn.turn_parts[0].signature == b"sig-13"
    assert turn.tool_calls[0].signature == b"call-sig-13"
    # 同一轮的载荷已经过期换掉了，证明它确实跨过了素材那道线
    assert MATERIAL_TRIMMED in _texts(ctx)


def test_a_round_that_goes_leaves_no_thought_behind():
    """整组过期整组走：不会留下一段没有调用、没有结果的思考。"""
    ctx = _play(
        start=_at(13, 0),
        until=_at(18, 10),
        events={
            "13:10": [
                _thought_round("s1", "search_online", "查一下这个", b"sig-13"),
                _result("s1", "网页正文"),
                _said("看完了"),
            ]
        },
    )

    assert [m for m in ctx if m.thought_text()] == []
    assert MATERIAL_TRIMMED not in _texts(ctx)


def test_a_round_with_a_picture_keeps_its_signatures():
    """带图那一轮：图换成一句话之后，这一轮的签名一个不少。"""
    call = ToolCall(
        id="p1", name="look_at_a_picture", arguments={}, signature=b"call-pic"
    )
    ctx = _play(
        start=_at(13, 0),
        until=_at(14, 10),
        events={
            "13:10": [
                Message.from_model_turn(
                    [
                        TurnPart.from_thought("看看这张", signature=b"sig-pic"),
                        TurnPart.from_tool_call(call),
                    ],
                    [call],
                ),
                _result(
                    "p1",
                    [
                        ContentBlock.from_text("pic=abc"),
                        ContentBlock.from_image_url({"url": _PNG}),
                    ],
                ),
            ]
        },
    )

    turn = next(m for m in ctx if m.turn_parts)
    assert turn.turn_parts[0].signature == b"sig-pic"
    assert turn.tool_calls[0].signature == b"call-pic"
    assert any(PICTURE_TRIMMED in t for t in _texts(ctx))
    assert _images(ctx) == []


def test_trimming_the_pictures_changes_the_content_and_nothing_else():
    """契约是「只换 content」，按 Message 的字段清单逐个比对。

    逐字段手抄的重建每加一个字段就多一次漏抄的机会，而漏抄的症状只落在带图那些轮次
    上：下一轮请求被 provider 拒掉。这条按 ``dataclasses.fields`` 比，以后加的字段
    自动在里面。
    """
    from dataclasses import fields

    from app.agent.continuity import _without_pictures

    call = ToolCall(
        id="p1",
        name="look_at_a_picture",
        arguments={"pic": "abc"},
        signature=b"call-pic",
    )
    message = Message(
        role=Role.ASSISTANT,
        content=[
            ContentBlock.from_text("pic=abc"),
            ContentBlock.from_image_url({"url": _PNG}),
        ],
        tool_calls=[call],
        tool_call_id="tc-1",
        turn_parts=[
            TurnPart.from_thought("看看这张", signature=b"sig-pic"),
            TurnPart.from_tool_call(call),
        ],
    )

    trimmed = _without_pictures(message)

    assert [b.type for b in trimmed.content] == ["text", "text"]
    assert trimmed.content[1].text == PICTURE_TRIMMED
    for f in fields(Message):
        if f.name == "content":
            continue
        assert getattr(trimmed, f.name) == getattr(message, f.name), f.name


# ---------------------------------------------------------------------------
# 九 · 跨天：日界没了之后，裁剪是唯一的收敛保证
#
# 上下文原来按生活日切，凌晨 4 点清空 —— 那一刀同时干了两件事：控制增长，和"她每天
# 重新开始"。第二件不是想要的性质（一个人不会每天早上忘掉昨天正在想的事），所以键上的
# 日期去掉了，loop 一直连着。
#
# 代价是**第一件事从此只剩裁剪一条路**：清零原来是唯一一次保证前缀重建的时刻，裁剪写
# 错了不会有每日自愈，症状是上下文只增不减，直到撞上模型的 context 上限、每一拍都在
# 同一个地方抛错。所以这一节验的是"连着跑好几天之后它仍然收敛"，不是顺带。
# ---------------------------------------------------------------------------


def _days(start: dt.datetime, days: int, *, policy: TrimPolicy = BEFORE_THE_MOVE) -> list[Message]:
    """连着跑 ``days`` 天，每天每 10 分钟一轮，中途穿插素材和图片。"""
    ctx: list[Message] = []
    at = start
    end = start + dt.timedelta(days=days)
    n = 0
    while at < end:
        if n % 18 == 0:  # 每 3 小时读一次网页
            produced = [
                _call("browse_online", f"b{n}", url="https://x"),
                _result(f"b{n}", "网页正文" * 200),
                _said("读到了点东西"),
            ]
        else:
            produced = [_said("继续")]
        ctx = _round(ctx, at, *produced, policy=policy)
        at += dt.timedelta(minutes=10)
        n += 1
    return ctx


def test_running_for_days_does_not_grow_without_bound():
    """连着跑五天，上下文不该比跑一天大到哪儿去 —— 没有日界之后这条只能靠裁剪。"""
    one = _days(_at(9), 1)
    five = _days(_at(9), 5)

    assert estimate_tokens(five) <= BEFORE_THE_MOVE.hard_cap_tokens
    assert estimate_tokens(five) < estimate_tokens(one) * 2, (
        f"一天 {estimate_tokens(one)} token，五天 {estimate_tokens(five)} —— "
        "它在按天累积，裁剪没有收敛"
    )


def test_crossing_four_in_the_morning_still_leaves_a_usable_context():
    """跨过 04:00 那一轮不再清零，但界桩、配对、编码都得照旧成立。"""
    ctx = _play(start=_at(2, 0, day=25), until=_at(6, 0, day=25))

    assert _orphans(ctx) == [], "跨过 04:00 之后留下了没有结果的调用"
    assert any(CHECKPOINT_HEAD in t for t in _texts(ctx)), "一根界桩都没有"


def test_what_she_said_yesterday_is_gone_by_today():
    """跨天不再清零，不等于昨天的话一直留着 —— 四小时那条线照旧生效。"""
    ctx = _play(
        start=_at(20, 0, day=25),
        until=_at(6, 0, day=26),
        events={"20:00": [_said("我昨晚正想着祭典的事")]},
    )

    assert "我昨晚正想着祭典的事" not in "\n".join(_texts(ctx))


def test_what_she_said_an_hour_ago_survives_crossing_four_in_the_morning():
    """而刚说过的那句必须活着穿过 04:00 —— 这正是去掉日界要换来的东西。

    这条和上一条是一对：只验"昨天的没了"的话，一个"每次跨 04:00 就清空"的实现
    照样能通过。
    """
    ctx = _play(
        start=_at(3, 0, day=26),
        until=_at(5, 0, day=26),
        events={"03:00": [_said("我正想着祭典的事")]},
    )

    assert "我正想着祭典的事" in "\n".join(_texts(ctx))


def test_the_first_round_on_the_new_key_lays_her_state_down():
    """部署那一下旧键上的历史接不过来 —— 新键第一次读到的是空的。

    这跟"一天的第一轮"走的是同一条路：历史空就立一根界桩，把她此刻的状态铺进去。
    所以这次切换的代价正好是"一次冷启动"，跟她过去每天早上 04:00 经历的那一次一样，
    不需要为它写一次性的搬运代码。
    """
    ctx = _round([], _at(14, 20), _said("继续"), state="手上：你在家/客厅，正在发呆。")

    assert CHECKPOINT_HEAD in ctx[0].text()
    assert "手上：你在家/客厅，正在发呆。" in ctx[0].text()


def test_which_returns_are_material_comes_from_the_caller():
    """哪些工具的返回算素材由**调用方**给，不是裁剪层写死的。

    写死的话下一个接上这套裁剪的调用方，它那几只手一只都不在表里 —— 全部走默认档、
    完整保留 240 分钟，并且没有任何报错。

    这条拿同一段历史跑两遍，只换分类表：``search_online`` 在表里就该褪成那句短语，
    不在表里就该原样留着。写死的实现过不了后半段。
    """
    material = {
        "13:30": [
            _call("search_online", "c1"),
            _result("c1", "搜到这些：抹茶店周一休息"),
            _said("知道了"),
        ]
    }

    faded = _play(start=_at(13, 0), until=_at(15, 0), events=material)
    intact = _play(
        start=_at(13, 0),
        until=_at(15, 0),
        events=material,
        material_tools=frozenset({"browse_online"}),  # 这张表里没有 search_online
    )

    def payload(ctx):
        got = [m for m in ctx if m.role is Role.TOOL and m.tool_call_id == "c1"]
        assert len(got) == 1, "调用还在保留期内，它的结果这条消息就必须还在"
        return got[0].text()

    assert payload(faded) == MATERIAL_TRIMMED
    assert payload(intact) == "搜到这些：抹茶店周一休息", (
        "换了分类表结果没变 —— 那张表是写死的"
    )
