"""连续上下文的契约用例 —— 键、日界、恢复、写失败、以及"谁不许裁剪"。

这个文件从她这一侧钉连续上下文（存取和裁剪的机制在基础层 :mod:`app.agent.continuity`，
键、写失败怎么算在 :mod:`app.living.moment`），逐条对应：

  1. 一个 persona 一条、键是 ``lane:persona``，不分天；
  2. 上下文写失败不拖垮这一轮，但留得下一行 ERROR，不是悄悄冷启动；
  3. moment 记录和手机已读是同一次提交，上下文在它之后单独写；
  4. 裁剪只有一处，存储层一根手指都不伸；
  5. 接回不靠内存 —— 上一个进程写下的上下文，这个进程读得回来。
"""

# ruff: noqa: F811 — 用例的形参名就是从 test_moment 借过来的 fixture 名
from __future__ import annotations

import datetime as dt
import logging

import pytest

from app.agent.continuity import (
    CHECKPOINT_HEAD,
    GAP_HEAD,
    commit_transcript,
)
from app.agent.neutral import ContentBlock, Message, Role, ToolCall, TurnPart
from app.agent.session import load_session
from app.data.session import get_session
from app.living.happening import record_happening
from app.living.moment import LifeMoment, latest_moment, run_moment, transcript_key
from app.living.received import ReceivedMessage, ReceivedRead
from app.living.records import KIND_SPEECH
from app.living.whereabouts import note_whereabouts
from app.runtime.persist import insert_idempotent
from tests.living.conftest import clock_at

# 真 pg + 替身 life 这两样跟逐个 moment 的用例是同一份，不在这里再抄一遍。
from tests.living.test_moment import moment_db, stub_moment  # noqa: F401

LANE = "coe-living"

_PHONE_CALL = ToolCall(
    id="c1",
    name="look_at_phone",
    arguments={"channel_id": "g1"},
    signature=b"\x00\xff gemini-thought",
)
_CST = dt.timezone(dt.timedelta(hours=8))


def _at(hour: int, minute: int = 0, day: int = 25) -> dt.datetime:
    return dt.datetime(2026, 7, day, hour, minute, tzinfo=_CST)


async def _stand(persona: str, place: str, doing: str, at: dt.datetime) -> None:
    await note_whereabouts(
        lane=LANE,
        persona_id=persona,
        moment_id=at.isoformat(timespec="minutes"),
        place=place,
        doing=doing,
        noted_at=at,
    )


async def _reaches_her(body: str, at: dt.datetime) -> None:
    """一条传到赤尾这里的消息，跟收件箱存下的一样（:func:`app.living.received.receive`）。"""
    await insert_idempotent(
        ReceivedMessage(
            lane=LANE,
            persona_id="akao",
            message_id=f"akao:{at.isoformat()}",
            sender="绫奈",
            body=body,
            message_time=at,
            wakes_recipient=True,
        )
    )


# 状态里「你刚做过、说过」那一段的标题（:meth:`app.living.snapshot.MomentSnapshot.render_state`）。
_OWN_HEAD = "你刚做过、说过"


def _is_checkpoint(message: Message) -> bool:
    """这条是界桩吗 —— 固定时刻清理立的，或者上一轮丢了补的那根。"""
    return isinstance(message.content, str) and message.content.startswith(
        (CHECKPOINT_HEAD, GAP_HEAD)
    )


def _stimuli(messages: list[Message]) -> list[Message]:
    """这些消息里属于"某一轮的刺激"的那几条。

    USER 消息现在有两种：每轮新摆到她眼前的那条刺激，和清理时立的那根界桩（一天的第
    一轮也立一根，因为全量状态只从界桩来）。"这一轮进了几次上下文"问的是前者。
    """
    return [
        m for m in messages if m.role is Role.USER and not _is_checkpoint(m)
    ]


async def _rows(data_cls) -> int:
    """这张表现在有几行。"""
    from sqlalchemy import text

    from app.runtime.migrator import _table_name

    async with get_session() as s:
        return (
            await s.execute(text(f"SELECT count(*) FROM {_table_name(data_cls)}"))
        ).scalar_one()


async def _write_transcript(
    transcript_id: str, messages: list[Message], *, expected_ver: int = 0
) -> None:
    """用例侧直接落一条上下文（模拟"上一个进程写下的"）。"""
    async with get_session() as s:
        await commit_transcript(
            transcript_id, messages, expected_ver=expected_ver, session=s
        )


# ---------------------------------------------------------------------------
# 一 · 键：一个人一条，不分天
#
# 这一节原来钉的是相反的事：键带生活日，凌晨 4 点上下文清零，跨天靠她自己写下的那一页
# 加挂心事加状态快照重铺接住。**去掉了。**
#
# 为什么：清零的两个职责里只有一个是真的。控制增长那一个由分层裁剪加 200k 硬顶接着做，
# 不需要每天砍一刀；另一个"她每天重新开始"不是想要的性质 —— 它让她每天早上忘掉昨天正在
# 想的事，而一个人不会。设计要的是长程 loop：她接着昨天往下想，前缀也因此跨天活着。
#
# 代价认下来：清零原来是**唯一一次保证前缀重建**的时刻，去掉之后裁剪必须长期正确，
# 写错了不会有每日自愈。所以下面第四节（跨天）和 ``test_context_trim.py`` 的收敛用例
# 是这条决定的配套，不是顺带。
# ---------------------------------------------------------------------------


def test_one_row_per_persona_forever():
    """同一个人永远落在同一条上 —— 早上、深夜、下个月，都是它。"""
    morning = transcript_key(lane=LANE, persona_id="akao")
    assert morning == f"{LANE}:akao"


def test_the_context_does_not_reset_at_four_in_the_morning():
    """凌晨 4 点她不该失忆。

    键带生活日的时候，04:01 那一轮读到的是一条空上下文：昨天正在想的事、正说到一半的
    话，全部只剩库里还有。接住它的是日记页那一层 —— 但那是一份她自己写的概述，不是
    她昨晚真正说过的每一句。现在两个时刻是同一条，她接着往下想。
    """
    before = transcript_key(lane=LANE, persona_id="akao")
    after = transcript_key(lane=LANE, persona_id="akao")
    assert before == after


def test_a_lane_and_a_person_never_share_a_row():
    ids = {
        transcript_key(lane=LANE, persona_id="akao"),
        transcript_key(lane=LANE, persona_id="ayana"),
        transcript_key(lane="prod", persona_id="akao"),
    }
    assert len(ids) == 3


def test_the_key_says_who_it_is_without_looking_anything_up():
    """排查的时候得一眼看出这条是谁的 —— 跟 langfuse 那个 session id 同一条理由。

    顺带钉住"键里没有日期"这件事本身：留着日期段的话它会静默地每天开一条新的，
    而症状是她每天早上不记得昨天，跟"裁剪太狠"长得一模一样。
    """
    tid = transcript_key(lane=LANE, persona_id="akao")
    assert tid.split(":") == [LANE, "akao"]


# ---------------------------------------------------------------------------
# 二 · 存取与恢复：接回不靠内存
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_missing_row_is_a_cold_start(moment_db):
    """从没写过 = 空上下文 + 版本 0，不是报错。"""
    assert await load_session(f"{LANE}:akao:2026-07-25") == ([], 0)


@pytest.mark.integration
async def test_what_another_process_wrote_comes_back_verbatim(moment_db):
    """重启接回：上一个进程写下的那条，这个进程一字不差读得回来。

    这里没有任何进程内缓存可言 —— 读的就是 PG 里最新那一版，所以"杀掉 pod 还能
    接着上次"这件事是结构性的。工具调用的 provider 私有 blob 和多模态返回都必须
    原样活下来，不然回放给模型的就是另一段对话。
    """
    tid = f"{LANE}:akao:2026-07-25"
    written = [
        Message(role=Role.USER, content="你醒了"),
        Message.from_model_turn(
            [
                TurnPart.from_thought("想了想", signature=b"\x01 thought-sig"),
                TurnPart.from_tool_call(_PHONE_CALL),
            ],
            [_PHONE_CALL],
        ),
        Message(
            role=Role.TOOL,
            content=[
                ContentBlock.from_text("绫奈：周末陪我去祭典"),
                ContentBlock.from_image_url({"url": "https://x/1.png"}),
            ],
            tool_call_id="c1",
        ),
        Message(role=Role.ASSISTANT, content="我回她一句"),
    ]
    await _write_transcript(tid, written)

    loaded, ver = await load_session(tid)
    assert ver == 1
    assert [m.role for m in loaded] == [
        Role.USER,
        Role.ASSISTANT,
        Role.TOOL,
        Role.ASSISTANT,
    ]
    assistant = loaded[1]
    assert assistant.thought_text() == "想了想"
    assert assistant.turn_parts[0].signature == b"\x01 thought-sig"
    assert assistant.tool_calls[0].signature == b"\x00\xff gemini-thought"
    assert loaded[2].content[1].image_url == {"url": "https://x/1.png"}


# ---------------------------------------------------------------------------
# 三 · 裁剪只有一处：存储层不插手
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_the_store_keeps_everything_it_is_handed(moment_db):
    """存储层不做任何截断。

    本设计的硬顶是 200k token，旧存储层那两条（200 条 / 256 KiB）比它小一个数量级，
    而且只记一行警告、不影响返回值 —— 两套同时生效的话她的话会被另一套规则悄悄砍掉，
    排查时看到的返回值一切正常。所以裁剪只留一处（:mod:`app.agent.continuity`），
    这里验存储层一根手指都不伸。
    """
    tid = f"{LANE}:akao:2026-07-25"
    filler = "x" * 4096  # 200 条 × 4 KiB ≈ 800 KiB，远超旧的 256 KiB
    written = [
        Message(role=Role.USER, content=f"m{i}-{filler}") for i in range(600)
    ]
    await _write_transcript(tid, written)

    loaded, _ver = await load_session(tid)
    assert len(loaded) == 600
    assert loaded[0].text() == f"m0-{filler}"
    assert loaded[-1].text() == f"m599-{filler}"


def test_the_storage_layer_has_no_caps_of_its_own():
    """旧的两条上限连名字都不该还在 —— 留着就会有人重新接上。"""
    import app.agent.session as store

    for gone in ("TRANSCRIPT_MAX_MESSAGES", "TRANSCRIPT_MAX_BYTES", "_cap_transcript"):
        assert not hasattr(store, gone), (
            f"{gone} 还在 —— 存储层又有了自己的一套裁剪，和 continuity 那套会同时生效"
        )


# ---------------------------------------------------------------------------
# 四 · 一个 moment 接着上一个往下走
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_the_next_moment_starts_from_the_stored_context(
    moment_db, stub_moment
):
    """第二个 moment 的输入里，第一个 moment 那条刺激和她的回答都在。"""
    runner = stub_moment(said="继续")
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14, 10)))

    first_input = runner.runs[0][0]
    second_input = runner.runs[1][0]
    assert len(first_input) == 2, "一天的第一个 moment：一根界桩 + 这一轮的刺激"
    assert _is_checkpoint(first_input[0])

    assert [m.content for m in second_input[:2]] == [
        m.content for m in first_input
    ], "第二个 moment 没接着第一个的那两条往下走"
    assert second_input[2].text() == "继续"
    assert second_input[-1].role is Role.USER
    assert second_input[-1].content != first_input[-1].content


@pytest.mark.integration
async def test_she_picks_up_a_context_this_process_never_wrote(
    moment_db, stub_moment
):
    """接回不靠内存：库里先有一段，这个进程第一个 moment 就接着它往下说。

    等价于"杀掉 pod 之后她接着之前的上下文继续" —— 新进程手上什么都没有，全部来自 PG。
    """
    tid = transcript_key(lane=LANE, persona_id="akao")
    await _write_transcript(
        tid,
        [
            Message(role=Role.USER, content="上一个进程喂进去的那条"),
            Message(role=Role.ASSISTANT, content="上一个进程里她说的那句"),
        ],
    )

    runner = stub_moment(said="继续")
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))

    fed = [m.text() for m in runner.runs[0][0]]
    assert fed[0] == "上一个进程喂进去的那条"
    assert fed[1] == "上一个进程里她说的那句"


@pytest.mark.integration
async def test_the_round_lands_in_the_context_exactly_once(moment_db, stub_moment):
    """一个 moment 跑完，这一轮的刺激 + 她的产出各进上下文一次。"""
    runner = stub_moment(
        ("keep_in_mind", {"still_on_my_mind": ["绫奈问我周末陪不陪她去祭典"]}),
        said="记下了",
    )
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))

    tid = transcript_key(lane=LANE, persona_id="akao")
    stored, ver = await load_session(tid)
    assert ver == 1
    # 一根界桩（一天的头一轮立的）+ 这一轮的刺激 + 工具那一组 + 最后那句
    assert [m.role for m in stored] == [
        Role.USER,
        Role.USER,
        Role.ASSISTANT,
        Role.TOOL,
        Role.ASSISTANT,
    ]
    assert _is_checkpoint(stored[0])
    assert stored[1].content == runner.runs[0][0][-1].content
    assert stored[-1].text() == "记下了"
    assert len(_stimuli(stored)) == 1


@pytest.mark.integration
async def test_she_carries_on_across_four_in_the_morning(moment_db, stub_moment):
    """04:00 一过她接着往下说，不是从空上下文重新开始。

    这条原来钉的是相反的事（"04:00 一过是新的一条"）。清零同时干了两件事，而只有一件
    是想要的：控制增长由分层裁剪加硬顶接着做；"她每天重新开始"不想要 —— 一个人不会在
    早上四点忘掉昨晚正在想的事。

    验的是**真的跑两轮**，不只是键算出来一样：键、读、写、裁剪四处只要有一处还按天分，
    她照样会在 04:00 失忆，而那种失忆跟"裁剪太狠"长得一模一样、事后分不出来。
    """
    runner = stub_moment(said="继续")
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(3, 50, day=26)))
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(4, 10, day=26)))

    # 第一轮：一根界桩 + 这一轮的刺激。
    assert len(runner.runs[0][0]) == 2
    # 第二轮：前面那一轮的每一条都还在（界桩、刺激、她说的那句），再接这一轮的刺激。
    second = runner.runs[1][0]
    assert len(second) > 2, "跨过 04:00 之后上下文空了 —— 她把昨晚忘干净了"
    assert _is_checkpoint(second[0])
    assert second[1].content == runner.runs[0][0][-1].content, (
        "第一轮那条刺激没带过来"
    )
    assert len(_stimuli(second)) == 2, "两轮的刺激应该都在这条上下文里"


# ---------------------------------------------------------------------------
# 五 · 上下文写失败 = 这一轮照样算数，但留得下痕迹
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_failed_context_write_leaves_the_round_standing(
    moment_db, stub_moment, monkeypatch, caplog
):
    """上下文写不进去时：这一轮照样算数，moment 记录和手机已读都落地，只留一行 ERROR。

    她这一轮已经开了口 —— 消息出了站、图上了传、手上的事换了。让这一轮失败回滚不掉
    这些，只会让下一拍重放：发送去重键带着 moment_id 和正文，换个措辞或跨一个时间格就
    对不上，她于是把同一句话对真人再说一遍。丢掉这一轮的上下文的代价小得多，而且不会
    静默：这里断言那一行 ERROR。
    """
    from tests.living.test_phone import _DM, _incoming, _seed_world

    await _seed_world()
    await _incoming(_DM, text_body="在吗", at=_at(13, 50))
    await _stand("akao", "家/客厅", "待着", _at(13))
    await _reaches_her("当面对你说：「你在看什么」", _at(13, 59))

    from app.living import moment as moment_mod

    async def boom(*_a, **_kw):
        raise RuntimeError("上下文写不进去")

    monkeypatch.setattr(moment_mod, "commit_transcript", boom)

    stub_moment(("look_at_phone", {"channel_id": str(_DM)}), said="继续")
    with caplog.at_level(logging.ERROR, logger="app.living.moment"):
        moment = await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))

    assert moment is not None, "上下文写失败把整轮拖垮了"
    landed = await latest_moment(lane=LANE, persona_id="akao")
    assert landed is not None and landed.moment_id == moment.moment_id
    assert await _rows(ReceivedRead) == 1, "收到的那条没跟着这一轮记成看过"

    from app.living.phone import PhoneRead

    assert await _rows(PhoneRead) == 1, "手机已读跟着上下文一起被回滚了"

    tid = transcript_key(lane=LANE, persona_id="akao")
    assert await load_session(tid) == ([], 0)
    assert any("上下文" in r.message for r in caplog.records), caplog.text


@pytest.mark.integration
async def test_a_failed_context_write_only_costs_her_this_round(
    moment_db, stub_moment, monkeypatch
):
    """丢掉的只有工具返回和中间过程 —— 她做过说过的事下一轮照样读得到。

    "你刚做过、说过"那段读的是库里的 ``Happening``，跟 ``LifeMoment`` 同一批已经提交，
    所以下一轮冷启动她仍然知道自己说过什么。
    """
    await _stand("akao", "家/客厅", "待着", _at(13))
    await _stand("ayana", "家/客厅", "看书", _at(13))

    from app.living import moment as moment_mod

    async def boom(*_a, **_kw):
        raise RuntimeError("上下文写不进去")

    real_commit = moment_mod.commit_transcript
    monkeypatch.setattr(moment_mod, "commit_transcript", boom)
    stub_moment(("say", {"what": "我去煮点抹茶。", "to": ["ayana"]}), said="去煮了")
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))

    monkeypatch.setattr(moment_mod, "commit_transcript", real_commit)
    runner = stub_moment(said="继续")
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14, 10)))

    fed = runner.runs[0][0]
    assert all(m.role is Role.USER for m in fed), (
        "上一轮没写进去，这一轮的历史里不该有她说过的话和工具返回"
    )
    assert "我去煮点抹茶。" in "\n".join(m.text() for m in fed), (
        "她连自己上一轮说过什么都不知道了 —— 那就不只是丢了工具返回"
    )


@pytest.mark.integration
async def test_a_lost_round_puts_her_state_back_in_front_of_her(
    moment_db, stub_moment, monkeypatch, caplog
):
    """一轮写成了、下一轮写失败，第三轮必须发现这个缺口并重铺一次状态。

    14:00 那轮立了界桩，14:10 那轮的上下文没写成，14:20 醒来读到的还是 14:00 那一版。
    没跨清理点，所以照旧不会重铺 —— 她于是接在一段过时的历史上，而刺激写着"离上一次
    过了 10 分钟"，指的是她眼前根本看不到的那一轮。
    """
    await _stand("akao", "家/客厅", "待着", _at(13))
    await _stand("ayana", "家/客厅", "看书", _at(13))

    from app.living import moment as moment_mod

    stub_moment(said="第一轮")
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))

    real_commit = moment_mod.commit_transcript

    async def boom(*_a, **_kw):
        raise RuntimeError("上下文写不进去")

    monkeypatch.setattr(moment_mod, "commit_transcript", boom)
    stub_moment(("say", {"what": "我去煮点抹茶。", "to": ["ayana"]}), said="第二轮")
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14, 10)))

    monkeypatch.setattr(moment_mod, "commit_transcript", real_commit)
    runner = stub_moment(said="第三轮")
    with caplog.at_level(logging.ERROR, logger="app.living.moment"):
        await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14, 20)))

    fed = runner.runs[0][0]
    assert [m.role for m in fed] == [
        Role.USER,       # 14:00 那根界桩
        Role.USER,       # 14:00 那轮的刺激
        Role.ASSISTANT,  # 14:00 那轮她说的
        Role.USER,       # 缺口那根：14:10 丢了，状态在这儿重铺
        Role.USER,       # 14:20 这一轮的刺激
    ], "上一轮丢了，这一轮照旧接着过时的历史往下说"
    assert fed[-2].text().startswith(GAP_HEAD), (
        "补的是清理那根界桩 —— 那句「再往前的那一段不在你眼前了」在这里是假话，"
        "往前那一段一条没少"
    )
    assert "我去煮点抹茶。" in fed[-2].text(), (
        "重铺的那条里没有她上一轮做过的事"
    )
    assert _OWN_HEAD in fed[-2].text(), (
        "缺口那根没带「你刚做过、说过」—— 丢掉那一轮说过的话比眼前剩下的都新，却不在眼前"
    )
    assert any("上一轮" in r.message for r in caplog.records), caplog.text


@pytest.mark.integration
async def test_a_crash_between_the_two_commits_is_found_by_the_next_round(
    moment_db, stub_moment, monkeypatch
):
    """进程崩在两次提交之间时连一行 ERROR 都留不下，缺口只能靠下一轮自己发现。

    moment 记录和手机已读已经提交、上下文那一步还没跑到就没了 —— 从下一轮读到的东西
    看，这跟"写失败"是同一个形状，所以判据必须是同一条，不能依赖写失败那条补偿路径。
    """
    await _stand("akao", "家/客厅", "待着", _at(13))
    await _stand("ayana", "家/客厅", "看书", _at(13))

    from app.living import moment as moment_mod

    stub_moment(said="第一轮")
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))

    real_remember = moment_mod._remember_this_round

    async def die(*_a, **_kw):
        raise RuntimeError("pod 没了")

    monkeypatch.setattr(moment_mod, "_remember_this_round", die)
    stub_moment(("say", {"what": "我去煮点抹茶。", "to": ["ayana"]}), said="第二轮")
    with pytest.raises(RuntimeError):
        await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14, 10)))

    monkeypatch.setattr(moment_mod, "_remember_this_round", real_remember)
    runner = stub_moment(said="第三轮")
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14, 20)))

    fed = runner.runs[0][0]
    assert [m.role for m in fed] == [
        Role.USER,
        Role.USER,
        Role.ASSISTANT,
        Role.USER,
        Role.USER,
    ], "崩在两次提交之间留下的缺口没被发现"
    assert "我去煮点抹茶。" in fed[-2].text()


@pytest.mark.integration
async def test_the_gap_is_only_reported_once(moment_db, stub_moment, monkeypatch):
    """缺口补上之后不再重铺：下一轮读到的版本已经追平这个 moment 记的那一版。"""
    await _stand("akao", "家/客厅", "待着", _at(13))

    from app.living import moment as moment_mod

    stub_moment(said="第一轮")
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))

    async def boom(*_a, **_kw):
        raise RuntimeError("上下文写不进去")

    real_commit = moment_mod.commit_transcript
    monkeypatch.setattr(moment_mod, "commit_transcript", boom)
    stub_moment(said="第二轮")
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14, 10)))

    monkeypatch.setattr(moment_mod, "commit_transcript", real_commit)
    stub_moment(said="第三轮")
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14, 20)))
    runner = stub_moment(said="第四轮")
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14, 30)))

    fed = runner.runs[0][0]
    assert [m.role for m in fed] == [
        Role.USER,       # 14:00 那根界桩
        Role.USER,       # 14:00 那轮的刺激
        Role.ASSISTANT,  # 14:00 那轮她说的
        Role.USER,       # 14:20 补上的那根
        Role.USER,       # 14:20 那轮的刺激
        Role.ASSISTANT,  # 14:20 那轮她说的
        Role.USER,       # 14:30 这一轮的刺激
    ], "缺口已经补上了，这一轮还在重铺状态"


@pytest.mark.integration
async def test_the_replay_after_a_failed_close_stores_the_round_once(
    moment_db, stub_moment, monkeypatch
):
    """收尾崩掉之后下一拍重跑：她重新看到那条，上下文里这一轮只留一份。

    重跑读到的历史跟上一次是同一份（上次什么都没提交），所以它是一次真正的重放，
    而不是"世界往前走了、她的上下文却停在原地"。
    """
    await _stand("akao", "家/客厅", "待着", _at(13))
    await _reaches_her("当面对你说：「你在看什么」", _at(13, 59))

    from app.living import moment as moment_mod

    real_insert = moment_mod.insert_idempotent

    async def crash(_row, **_kw):
        raise RuntimeError("崩")

    runner = stub_moment(said="继续")
    monkeypatch.setattr(moment_mod, "insert_idempotent", crash)
    with pytest.raises(RuntimeError):
        await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14, 0)))

    monkeypatch.setattr(moment_mod, "insert_idempotent", real_insert)
    again = await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14, 1)))

    assert again is not None
    replayed = runner.runs[-1][0]
    assert len(replayed) == 2, "重放读到的历史不该带上没提交的那一轮"
    assert "你在看什么" in replayed[-1].content, "崩掉那一轮看到的那条被静默吞了"

    tid = transcript_key(lane=LANE, persona_id="akao")
    stored, ver = await load_session(tid)
    assert ver == 1
    assert len(_stimuli(stored)) == 1


@pytest.mark.integration
async def test_the_same_summons_only_wakes_her_once(moment_db, stub_moment):
    """同一条消息把她叫来两次时，第二次一句模型都不调，上下文也不多一条。"""
    runner = stub_moment(said="继续")
    first = await run_moment(
        lane=LANE, persona_id="akao", clock=clock_at(_at(14)), nudged_by="msg-1"
    )
    second = await run_moment(
        lane=LANE, persona_id="akao", clock=clock_at(_at(14, 2)), nudged_by="msg-1"
    )

    assert first is not None
    assert second is None
    assert len(runner.runs) == 1

    tid = transcript_key(lane=LANE, persona_id="akao")
    stored, ver = await load_session(tid)
    assert ver == 1
    assert len(_stimuli(stored)) == 1


@pytest.mark.integration
async def test_the_moment_record_and_the_context_land_together(
    moment_db, stub_moment
):
    """一个 moment 落地了，它的上下文一定也落地了 —— 同一次提交。"""
    stub_moment(said="继续")
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))

    async with get_session() as s:
        from sqlalchemy import text

        from app.runtime.migrator import _table_name

        rows = (
            await s.execute(
                text(f"SELECT count(*) FROM {_table_name(LifeMoment)}")
            )
        ).scalar_one()
    assert rows == 1

    tid = transcript_key(lane=LANE, persona_id="akao")
    _stored, ver = await load_session(tid)
    assert ver == 1


# ---------------------------------------------------------------------------
# 六 · 「你刚做过、说过」只在她自己的话不在眼前时给
#
# 其余时候她 4 小时内说过的话、发出去的每一条都原样在上下文里；每小时的状态块再各抄
# 一份最近 12 句，2026-09-23 她一轮输入里自己的话于是出现了几十次。判据由组装那一层
# 给（:func:`app.agent.continuity.continues_unbroken`）：这一轮的历史里没有任何标记
# 消息，或者上一轮没存下来。上下文不按天清零，所以正常跑着的时候这一段不出现，跨过
# 04:00 也一样；它只在上下文是空的、断过的时候出现。
# ---------------------------------------------------------------------------


def _everything_she_read(fed: list[Message]) -> str:
    """她这一轮眼前的全部文字，连她自己那几次调用的参数一起。"""
    import json

    return "\n".join(
        [m.text() for m in fed]
        + [json.dumps(c.arguments, ensure_ascii=False) for m in fed for c in m.tool_calls]
    )


async def _she_said_last_night() -> None:
    await _stand("akao", "家/我房间", "准备睡了", _at(23, 40))
    await record_happening(
        lane=LANE,
        happening_id="akao-night",
        actor="akao",
        kind=KIND_SPEECH,
        content="晚安，明天见。",
        occurred_at=_at(23, 50),
        audience=["绫奈"],
    )


async def _lose_the_context_of_one_round(monkeypatch, run) -> None:
    """跑一轮，但这一轮的上下文写不进去（:func:`app.living.moment._remember_this_round`）。"""
    from app.living import moment as moment_mod

    real_commit = moment_mod.commit_transcript

    async def boom(*_a, **_kw):
        raise RuntimeError("上下文写不进去")

    monkeypatch.setattr(moment_mod, "commit_transcript", boom)
    await run()
    monkeypatch.setattr(moment_mod, "commit_transcript", real_commit)


@pytest.mark.integration
async def test_an_empty_context_shows_her_what_she_said_before_it(
    moment_db, stub_moment
):
    """上下文是空的（第一次跑、清过库），她之前说过的话只能从这一段来。

    这是有意保留的：那一轮这几句就是她眼前唯一的说话样本，会把之前的腔调带进来 ——
    可她得记得自己说过什么。
    """
    await _she_said_last_night()

    runner = stub_moment(said="继续")
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(9, day=26)))

    first = runner.runs[0][0][0]
    assert _is_checkpoint(first)
    assert _OWN_HEAD in first.text() and "晚安，明天见。" in first.text(), first.text()


@pytest.mark.integration
async def test_crossing_four_in_the_morning_does_not_copy_her_words_again(
    moment_db, stub_moment
):
    """跨过 04:00 那根标记消息跟别的整点一样，不带「你刚做过、说过」。

    上下文不按天清零（:func:`app.living.moment.transcript_key` 不分天），她昨晚说过的话
    还原样在上下文里，再抄一份就是重复。
    """
    await _stand("akao", "家/我房间", "准备睡了", _at(3, 30, day=26))
    stub_moment(("say", {"what": "晚安，明天见。", "to": ["ayana"]}), said="睡了")
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(3, 50, day=26)))

    runner = stub_moment(said="继续")
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(4, 10, day=26)))

    fed = runner.runs[0][0]
    marker = fed[-2]
    assert marker.text().startswith(f"{CHECKPOINT_HEAD}{_at(4, day=26).isoformat()}")
    assert _OWN_HEAD not in marker.text(), marker.text()
    assert _everything_she_read(fed).count("晚安，明天见。") == 1, (
        "她这句话在这一轮眼前不止一处 —— 除了她自己那次调用，别处都是重复"
    )


@pytest.mark.integration
async def test_an_hourly_cleanup_does_not_copy_her_words_again(
    moment_db, stub_moment
):
    """每小时清理那一下不再带「你刚做过、说过」：她说过的话只以她自己那次调用出现一次。"""
    await _stand("akao", "家/客厅", "待着", _at(13))
    stub_moment(("say", {"what": "我去煮点抹茶。", "to": ["ayana"]}), said="去煮了")
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14, 30)))

    runner = stub_moment(said="继续")
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(15)))

    fed = runner.runs[0][0]
    cleanup = fed[-2]
    assert cleanup.text().startswith(f"{CHECKPOINT_HEAD}{_at(15).isoformat()}")
    assert _OWN_HEAD not in cleanup.text(), (
        f"每小时的清理又抄了一遍她最近说过的话。拿到：\n{cleanup.text()}"
    )
    assert _everything_she_read(fed).count("我去煮点抹茶。") == 1, (
        "她这句话在这一轮眼前不止一处 —— 除了她自己那次调用，别处都是重复"
    )


@pytest.mark.integration
async def test_a_lost_round_right_at_a_cleanup_still_shows_her_what_she_said(
    moment_db, stub_moment, monkeypatch
):
    """缺口跟清理点撞在一起时立的是清理那根，照样带「你刚做过、说过」。

    判据不看这一轮立的是哪种标记消息：清理点先判，所以这一下立的是清理那根，但丢掉
    那一轮说过的话照样不在她眼前。
    """
    await _stand("akao", "家/客厅", "待着", _at(13))
    await _stand("ayana", "家/客厅", "看书", _at(13))

    stub_moment(said="第一轮")
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14, 40)))

    stub_moment(("say", {"what": "我去煮点抹茶。", "to": ["ayana"]}), said="第二轮")
    await _lose_the_context_of_one_round(
        monkeypatch,
        lambda: run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14, 50))),
    )

    runner = stub_moment(said="第三轮")
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(15)))

    marker = runner.runs[0][0][-2]
    assert marker.text().startswith(CHECKPOINT_HEAD), "用例前提：清理那根赢了"
    assert _OWN_HEAD in marker.text() and "我去煮点抹茶。" in marker.text(), (
        f"缺口撞上清理点，丢掉那一轮说过的话就没人给了。拿到：\n{marker.text()}"
    )


@pytest.mark.integration
async def test_after_the_hard_cap_took_every_marker_she_is_shown_what_she_said(
    moment_db, stub_moment, monkeypatch
):
    """硬顶把标记消息全裁掉之后，下一轮立的是清理那根，照样带「你刚做过、说过」。

    剩下那一截历史说不清是从哪儿接上的 —— 硬顶从最老的组开始整组丢，丢掉的可能正是
    她刚说过的话。
    """
    from app.agent.continuity import TrimPolicy
    from app.living import moment as moment_mod

    tiny = TrimPolicy(
        material_minutes=60,
        own_minutes=240,
        cleanup_minutes=60,
        hard_cap_tokens=50,
        trim_target_tokens=20,
    )
    monkeypatch.setattr(moment_mod, "MOMENT_TRIM_POLICY", tiny)
    await _stand("akao", "家/客厅", "待着", _at(13))

    stub_moment(("say", {"what": "我去煮点抹茶。", "to": ["ayana"]}), said="去煮了")
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))
    stored, _ver = await load_session(transcript_key(lane=LANE, persona_id="akao"))
    assert not any(_is_checkpoint(m) for m in stored), "用例前提：硬顶把标记消息裁掉了"

    runner = stub_moment(said="继续")
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14, 10)))

    marker = runner.runs[0][0][-2]
    assert _is_checkpoint(marker)
    assert _OWN_HEAD in marker.text() and "我去煮点抹茶。" in marker.text(), (
        f"硬顶之后这一轮没带她最近说过的话。拿到：\n{marker.text()}"
    )


@pytest.mark.integration
async def test_what_a_gap_marker_brought_back_goes_at_the_next_cleanup(
    moment_db, stub_moment, monkeypatch
):
    """**有意接受的提前遗忘**：缺口那根补回来的话，下一个清理点连同正文一起折叠掉。

    丢掉那一轮说过的话只在缺口那根的状态里有；下一个清理点那根折叠，新立的清理标记又
    不带「你刚做过、说过」（历史里有标记消息、上一轮也存下来了），所以最早 50 分钟之后
    她就看不到那几句了，而不是 4 小时。接受的理由：缺口只在上下文写失败或进程崩溃时出
    现；那几句仍在她自己的记录和日记材料里，发出去的消息在手机页上也还有编号。为这种
    情况让缺口正文不折叠，会重新引入重复和特例。
    """
    await _stand("akao", "家/客厅", "待着", _at(13))
    await _stand("ayana", "家/客厅", "看书", _at(13))

    stub_moment(said="第一轮")
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14)))

    stub_moment(("say", {"what": "我去煮点抹茶。", "to": ["ayana"]}), said="第二轮")
    await _lose_the_context_of_one_round(
        monkeypatch,
        lambda: run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14, 10))),
    )

    gap_round = stub_moment(said="第三轮")
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(14, 20)))
    assert "我去煮点抹茶。" in _everything_she_read(gap_round.runs[0][0]), (
        "用例前提：缺口那根把丢掉那一轮说过的话补回来了"
    )

    runner = stub_moment(said="第四轮")
    await run_moment(lane=LANE, persona_id="akao", clock=clock_at(_at(15)))

    assert "我去煮点抹茶。" not in _everything_she_read(runner.runs[0][0]), (
        "缺口那根的正文过了清理点还在 —— 那就又是一份叠着的状态"
    )
