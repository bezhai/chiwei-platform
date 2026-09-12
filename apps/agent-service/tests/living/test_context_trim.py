"""分层裁剪 —— 她的上下文在固定时刻按两档时长裁，全程不做总结。

这个文件钉 :mod:`app.living.continuity` 裁剪那一半的每一条：

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

import pytest

from app.agent.neutral import ContentBlock, Message, Role, ToolCall
from app.living.continuity import (
    CHECKPOINT_HEAD,
    DEFAULT_TRIM_POLICY,
    KEPT_TOOLS,
    MATERIAL_TOOLS,
    MATERIAL_TRIMMED,
    PICTURE_TRIMMED,
    TrimPolicy,
    estimate_tokens,
    load_moment_transcript,
    load_trim_policy,
    moment_transcript_id,
    next_transcript,
    trim_for_round,
)
from app.living.moment import MOMENT_TOOLS, run_moment
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

POLICY = TrimPolicy(
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
    policy: TrimPolicy = POLICY,
) -> list[Message]:
    """跑一轮：先按这一刻裁一遍历史，再把这一轮的输入和产出接上去。

    两步的顺序跟 :func:`app.living.moment.run_moment` 一样 —— 裁在模型调用之前，
    存下去的就是喂进去的那份加上这一轮。
    """
    kept = trim_for_round(history, now=at, state=state, policy=policy)
    return next_transcript(
        kept, [_stim(f"现在 {at:%H:%M}。"), *produced], policy=policy
    )


def _play(
    *,
    start: dt.datetime,
    until: dt.datetime,
    events: dict[str, list[Message]] | None = None,
    policy: TrimPolicy = POLICY,
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
        ctx = _round(ctx, at, *produced, policy=policy)
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
    from app.living.continuity import MAX_CLEANUP_MINUTES, PICTURE_URL_MINUTES
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
    with caplog.at_level(logging.WARNING, logger="app.living.continuity"):
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
    with caplog.at_level(logging.ERROR, logger="app.living.continuity"):
        ctx = _round([], _at(14, 5), huge, policy=policy)

    assert huge.text() in _texts(ctx)
    assert any("硬顶" in r.message for r in caplog.records), caplog.text


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


# ---------------------------------------------------------------------------
# 七 · 阈值全部走动态配置
# ---------------------------------------------------------------------------


class _FakeConfig:
    def __init__(self, values: dict[str, int]) -> None:
        self.values = values
        self.asked: list[str] = []

    def get_int(self, key: str, *, default: int = 0) -> int:
        self.asked.append(key)
        return self.values.get(key, default)


async def test_every_threshold_comes_from_dynamic_config(monkeypatch):
    """五个阈值运行时都能改，一个都不写死在源码里。"""
    from app.living import continuity as mod

    fake = _FakeConfig(
        {
            mod.MATERIAL_MINUTES_KEY: 30,
            mod.OWN_MINUTES_KEY: 120,
            mod.CLEANUP_MINUTES_KEY: 20,
            mod.HARD_CAP_TOKENS_KEY: 90_000,
            mod.TRIM_TARGET_TOKENS_KEY: 40_000,
        }
    )
    monkeypatch.setattr(mod, "dynamic_config", fake)

    policy = await load_trim_policy()
    assert policy == TrimPolicy(
        material_minutes=30,
        own_minutes=120,
        cleanup_minutes=20,
        hard_cap_tokens=90_000,
        trim_target_tokens=40_000,
    )
    assert set(fake.asked) == {
        mod.MATERIAL_MINUTES_KEY,
        mod.OWN_MINUTES_KEY,
        mod.CLEANUP_MINUTES_KEY,
        mod.HARD_CAP_TOKENS_KEY,
        mod.TRIM_TARGET_TOKENS_KEY,
    }


async def test_nothing_configured_is_the_documented_default(monkeypatch):
    from app.living import continuity as mod

    monkeypatch.setattr(mod, "dynamic_config", _FakeConfig({}))
    assert await load_trim_policy() == DEFAULT_TRIM_POLICY


@pytest.mark.parametrize(
    "bad",
    [
        {"living_context_own_minutes": 30},  # 她自己的话比素材还短
        {"living_context_cleanup_minutes": 0},
        {"living_context_cleanup_minutes": 600},  # 比签名寿命还长，图片会烂在里面
        {"living_context_trim_target_tokens": 300_000},  # 目标比硬顶还大
        {"living_context_material_minutes": -1},
    ],
)
async def test_a_threshold_that_cannot_hold_falls_back(monkeypatch, bad, caplog):
    """配脏了就退回默认值并说一声，不拿一个自相矛盾的策略去裁她的记忆。"""
    from app.living import continuity as mod

    monkeypatch.setattr(mod, "dynamic_config", _FakeConfig(bad))
    with caplog.at_level(logging.WARNING, logger="app.living.continuity"):
        assert await load_trim_policy() == DEFAULT_TRIM_POLICY
    assert caplog.records


def test_the_defaults_are_the_shape_the_design_asked_for():
    assert DEFAULT_TRIM_POLICY.material_minutes == 60
    assert DEFAULT_TRIM_POLICY.own_minutes == 240
    assert DEFAULT_TRIM_POLICY.cleanup_minutes == 60
    assert DEFAULT_TRIM_POLICY.hard_cap_tokens == 200_000
    assert DEFAULT_TRIM_POLICY.trim_target_tokens == 100_000


# ---------------------------------------------------------------------------
# 八 · 真的跑一轮：裁剪落在存下来的那一份上
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_cleanup_lands_in_the_stored_transcript(
    moment_db, stub_moment, monkeypatch
):
    """真跑三个 moment 跨过一个清理点：存下来的那一份里状态重铺过、素材换成了短语。

    第二个 moment 立起第一根界桩，第三个才有得算年龄 —— 这就是一天头几轮的样子。
    """
    from app.living import moment as moment_mod

    async def fixed_policy() -> TrimPolicy:
        return POLICY

    monkeypatch.setattr(moment_mod, "load_trim_policy", fixed_policy)

    stub_moment(("look_around", {}), said="继续")
    await run_moment(lane=LANE, persona_id="akao", now=_at(13, 50))

    stub_moment(said="继续")
    await run_moment(lane=LANE, persona_id="akao", now=_at(14, 0))

    stub_moment(said="继续")
    await run_moment(lane=LANE, persona_id="akao", now=_at(15, 10))

    tid = moment_transcript_id(lane=LANE, persona_id="akao", now=_at(15, 10))
    stored, _ver = await load_moment_transcript(tid)
    joined = "\n".join(_texts(stored))

    assert MATERIAL_TRIMMED in joined, "look_around 的返回是素材，跨过清理点该换掉"
    assert _orphans(stored) == []
    assert CHECKPOINT_HEAD in joined, "清理那一下要把她当时的状态重铺进去"


@pytest.mark.integration
async def test_a_picture_is_gone_before_she_is_fed_again(
    moment_db, stub_moment, monkeypatch
):
    """停机跨过签名寿命再起来：喂给模型的那份里已经没有图片块了。

    裁剪如果只发生在收尾，这条路永远走不到 —— adapter 会在模型请求之前拿 403 抛错，
    每一轮都炸在同一个地方，收尾轮不上。所以裁必须在喂之前。
    """
    from app.data.session import get_session
    from app.living import moment as moment_mod
    from app.living.continuity import commit_moment_transcript

    async def fixed_policy() -> TrimPolicy:
        return POLICY

    monkeypatch.setattr(moment_mod, "load_trim_policy", fixed_policy)

    # 先塞一段带图片的历史，模拟上一个进程 13:00 那一轮画过一张
    tid = moment_transcript_id(lane=LANE, persona_id="akao", now=_at(13, 0))

    async with get_session() as s:
        await commit_moment_transcript(
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
    await run_moment(lane=LANE, persona_id="akao", now=_at(15, 10))

    fed = runner.runs[0][0]
    assert _images(fed) == [], "过期地址在模型看到它之前就该没了"
    assert "pic=abc123" in "".join(_texts(fed)), "句柄还得在"
