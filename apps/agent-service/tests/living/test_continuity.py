"""连续上下文的契约用例 —— 键、日界、恢复、写失败、以及"谁不许裁剪"。

这个文件钉的是 :mod:`app.living.continuity` 那份契约的每一条，逐条对应：

  1. 一个 persona 一条、键是 ``lane:persona:生活日``，日界在 CST 04:00；
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

from app.agent.neutral import ContentBlock, Message, Role, ToolCall
from app.data.session import get_session
from app.living.continuity import (
    CHECKPOINT_HEAD,
    TranscriptConflict,
    commit_moment_transcript,
    load_moment_transcript,
    moment_transcript_id,
)
from app.living.happening import record_happening
from app.living.moment import LifeMoment, latest_moment, run_moment
from app.living.records import KIND_SPEECH
from app.living.whereabouts import note_whereabouts

# 真 pg + 替身 life 这两样跟逐个 moment 的用例是同一份，不在这里再抄一遍。
from tests.living.test_moment import moment_db, stub_moment  # noqa: F401

LANE = "coe-living"
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


def _is_checkpoint(message: Message) -> bool:
    return isinstance(message.content, str) and message.content.startswith(
        CHECKPOINT_HEAD
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
        await commit_moment_transcript(
            transcript_id, messages, expected_ver=expected_ver, session=s
        )


# ---------------------------------------------------------------------------
# 一 · 键与日界
# ---------------------------------------------------------------------------


def test_one_row_per_persona_for_the_whole_living_day():
    """同一个人同一个生活日内的每一个 moment 都落在同一条上。"""
    morning = moment_transcript_id(lane=LANE, persona_id="akao", now=_at(9))
    night = moment_transcript_id(lane=LANE, persona_id="akao", now=_at(23, 50))
    assert morning == night == f"{LANE}:akao:2026-07-25"


def test_the_day_rolls_at_four_in_the_morning_not_at_midnight():
    """凌晨两点她还醒着，上下文不能在午夜断掉。

    日界按自然日切的话，23:58 和 00:02 是两条 —— 她那一段连着的经历被从中间劈开，
    而她自己没有任何理由感觉到这个边界。生活日的边界是 04:00。
    """
    before_midnight = moment_transcript_id(
        lane=LANE, persona_id="akao", now=_at(23, 58, day=25)
    )
    after_midnight = moment_transcript_id(
        lane=LANE, persona_id="akao", now=_at(2, 30, day=26)
    )
    assert before_midnight == after_midnight

    just_before_four = moment_transcript_id(
        lane=LANE, persona_id="akao", now=_at(3, 59, day=26)
    )
    just_after_four = moment_transcript_id(
        lane=LANE, persona_id="akao", now=_at(4, 1, day=26)
    )
    assert just_before_four != just_after_four


def test_a_lane_and_a_person_never_share_a_row():
    ids = {
        moment_transcript_id(lane=LANE, persona_id="akao", now=_at(9)),
        moment_transcript_id(lane=LANE, persona_id="ayana", now=_at(9)),
        moment_transcript_id(lane="prod", persona_id="akao", now=_at(9)),
    }
    assert len(ids) == 3


# ---------------------------------------------------------------------------
# 二 · 存取与恢复：接回不靠内存
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_missing_row_is_a_cold_start(moment_db):
    """从没写过 = 空上下文 + 版本 0，不是报错。"""
    assert await load_moment_transcript(f"{LANE}:akao:2026-07-25") == ([], 0)


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
        Message(
            role=Role.ASSISTANT,
            content="",
            reasoning_content="想了想",
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="look_at_phone",
                    arguments={"channel_id": "g1"},
                    signature=b"\x00\xff gemini-thought",
                )
            ],
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

    loaded, ver = await load_moment_transcript(tid)
    assert ver == 1
    assert [m.role for m in loaded] == [
        Role.USER,
        Role.ASSISTANT,
        Role.TOOL,
        Role.ASSISTANT,
    ]
    assistant = loaded[1]
    assert assistant.reasoning_content == "想了想"
    assert assistant.tool_calls[0].signature == b"\x00\xff gemini-thought"
    assert loaded[2].content[1].image_url == {"url": "https://x/1.png"}


@pytest.mark.integration
async def test_writing_from_a_stale_version_is_refused(moment_db):
    """读到哪一版就只能覆盖哪一版。

    进程内的排他占用保证同一个人不会两个 moment 同时跑，所以正常永远撞不上这条。
    撞上就说明那个前提破了（多副本、或者有人绕开了占用），这时候必须炸 —— 默默
    覆盖等于把另一个进程刚写下的一整段丢掉，而且查不出来。
    """
    tid = f"{LANE}:akao:2026-07-25"
    await _write_transcript(tid, [Message(role=Role.USER, content="第一版")])

    with pytest.raises(TranscriptConflict):
        await _write_transcript(
            tid, [Message(role=Role.USER, content="拿旧版本覆盖")], expected_ver=0
        )

    loaded, ver = await load_moment_transcript(tid)
    assert ver == 1
    assert [m.text() for m in loaded] == ["第一版"]


# ---------------------------------------------------------------------------
# 三 · 裁剪只有一处：存储层不插手
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_the_store_keeps_everything_it_is_handed(moment_db):
    """存储层不做任何截断。

    本设计的硬顶是 200k token，旧存储层那两条（200 条 / 256 KiB）比它小一个数量级，
    而且只记一行警告、不影响返回值 —— 两套同时生效的话她的话会被另一套规则悄悄砍掉，
    排查时看到的返回值一切正常。所以裁剪只留一处（:mod:`app.living.continuity`），
    这里验存储层一根手指都不伸。
    """
    tid = f"{LANE}:akao:2026-07-25"
    filler = "x" * 4096  # 200 条 × 4 KiB ≈ 800 KiB，远超旧的 256 KiB
    written = [
        Message(role=Role.USER, content=f"m{i}-{filler}") for i in range(600)
    ]
    await _write_transcript(tid, written)

    loaded, _ver = await load_moment_transcript(tid)
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
    await run_moment(lane=LANE, persona_id="akao", now=_at(14))
    await run_moment(lane=LANE, persona_id="akao", now=_at(14, 10))

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
    tid = moment_transcript_id(lane=LANE, persona_id="akao", now=_at(14))
    await _write_transcript(
        tid,
        [
            Message(role=Role.USER, content="上一个进程喂进去的那条"),
            Message(role=Role.ASSISTANT, content="上一个进程里她说的那句"),
        ],
    )

    runner = stub_moment(said="继续")
    await run_moment(lane=LANE, persona_id="akao", now=_at(14))

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
    await run_moment(lane=LANE, persona_id="akao", now=_at(14))

    tid = moment_transcript_id(lane=LANE, persona_id="akao", now=_at(14))
    stored, ver = await load_moment_transcript(tid)
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
async def test_the_context_starts_over_at_the_living_day_boundary(
    moment_db, stub_moment
):
    """04:00 一过是新的一条，她不会把昨天的对话原样拖进今天。"""
    runner = stub_moment(said="继续")
    await run_moment(lane=LANE, persona_id="akao", now=_at(3, 50, day=26))
    await run_moment(lane=LANE, persona_id="akao", now=_at(4, 10, day=26))

    # 每一天的第一个 moment 都是"一根界桩 + 这一轮的刺激"，昨天那一段一条都不带过来
    assert len(runner.runs[0][0]) == 2
    assert len(runner.runs[1][0]) == 2, "跨过 04:00 还接着昨天那条"


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
    await _stand("ayana", "家/客厅", "看书", _at(13))
    await record_happening(
        lane=LANE,
        happening_id="ay-x",
        actor="ayana",
        place="家/客厅",
        kind=KIND_SPEECH,
        content="你在看什么",
        occurred_at=_at(13, 59),
        audience=["akao"],
    )

    from app.living import moment as moment_mod

    async def boom(*_a, **_kw):
        raise RuntimeError("上下文写不进去")

    monkeypatch.setattr(moment_mod, "commit_moment_transcript", boom)

    stub_moment(("look_at_phone", {"channel_id": str(_DM)}), said="继续")
    with caplog.at_level(logging.ERROR, logger="app.living.moment"):
        moment = await run_moment(lane=LANE, persona_id="akao", now=_at(14))

    assert moment is not None, "上下文写失败把整轮拖垮了"
    landed = await latest_moment(lane=LANE, persona_id="akao")
    assert landed is not None and landed.moment_id == moment.moment_id
    assert landed.next_seq > 0, "游标没跟着这一轮推进"

    from app.living.phone import PhoneRead

    assert await _rows(PhoneRead) == 1, "手机已读跟着上下文一起被回滚了"

    tid = moment_transcript_id(lane=LANE, persona_id="akao", now=_at(14))
    assert await load_moment_transcript(tid) == ([], 0)
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

    real_commit = moment_mod.commit_moment_transcript
    monkeypatch.setattr(moment_mod, "commit_moment_transcript", boom)
    stub_moment(("say", {"what": "我去煮点抹茶。", "to": ["ayana"]}), said="去煮了")
    await run_moment(lane=LANE, persona_id="akao", now=_at(14))

    monkeypatch.setattr(moment_mod, "commit_moment_transcript", real_commit)
    runner = stub_moment(said="继续")
    await run_moment(lane=LANE, persona_id="akao", now=_at(14, 10))

    fed = runner.runs[0][0]
    assert all(m.role is Role.USER for m in fed), (
        "上一轮没写进去，这一轮的历史里不该有她说过的话和工具返回"
    )
    assert "我去煮点抹茶。" in "\n".join(m.text() for m in fed), (
        "她连自己上一轮说过什么都不知道了 —— 那就不只是丢了工具返回"
    )


@pytest.mark.integration
async def test_the_replay_after_a_failed_close_stores_the_round_once(
    moment_db, stub_moment, monkeypatch
):
    """收尾崩掉之后下一拍重跑：她重新感知那条，上下文里这一轮只留一份。

    重跑读到的历史跟上一次是同一份（上次什么都没提交），所以它是一次真正的重放，
    而不是"世界往前走了、她的上下文却停在原地"。
    """
    await _stand("akao", "家/客厅", "待着", _at(13))
    await _stand("ayana", "家/客厅", "看书", _at(13))
    await record_happening(
        lane=LANE,
        happening_id="ay-x",
        actor="ayana",
        place="家/客厅",
        kind=KIND_SPEECH,
        content="你在看什么",
        occurred_at=_at(13, 59),
        audience=["akao"],
    )

    from app.living import moment as moment_mod

    real_insert = moment_mod.insert_idempotent

    async def crash(_row, **_kw):
        raise RuntimeError("崩")

    runner = stub_moment(said="继续")
    monkeypatch.setattr(moment_mod, "insert_idempotent", crash)
    with pytest.raises(RuntimeError):
        await run_moment(lane=LANE, persona_id="akao", now=_at(14, 0))

    monkeypatch.setattr(moment_mod, "insert_idempotent", real_insert)
    again = await run_moment(lane=LANE, persona_id="akao", now=_at(14, 1))

    assert again is not None
    assert again.after_seq == 0, "崩掉那一轮的感知被静默吞了"
    replayed = runner.runs[-1][0]
    assert len(replayed) == 2, "重放读到的历史不该带上没提交的那一轮"
    assert "你在看什么" in replayed[-1].content

    tid = moment_transcript_id(lane=LANE, persona_id="akao", now=_at(14, 1))
    stored, ver = await load_moment_transcript(tid)
    assert ver == 1
    assert len(_stimuli(stored)) == 1


@pytest.mark.integration
async def test_the_same_summons_only_wakes_her_once(moment_db, stub_moment):
    """同一条消息把她叫来两次时，第二次一句模型都不调，上下文也不多一条。"""
    runner = stub_moment(said="继续")
    first = await run_moment(
        lane=LANE, persona_id="akao", now=_at(14), nudged_by="msg-1"
    )
    second = await run_moment(
        lane=LANE, persona_id="akao", now=_at(14, 2), nudged_by="msg-1"
    )

    assert first is not None
    assert second is None
    assert len(runner.runs) == 1

    tid = moment_transcript_id(lane=LANE, persona_id="akao", now=_at(14))
    stored, ver = await load_moment_transcript(tid)
    assert ver == 1
    assert len(_stimuli(stored)) == 1


@pytest.mark.integration
async def test_the_moment_record_and_the_context_land_together(
    moment_db, stub_moment
):
    """一个 moment 落地了，它的上下文一定也落地了 —— 同一次提交。"""
    stub_moment(said="继续")
    await run_moment(lane=LANE, persona_id="akao", now=_at(14))

    async with get_session() as s:
        from sqlalchemy import text

        from app.runtime.migrator import _table_name

        rows = (
            await s.execute(
                text(f"SELECT count(*) FROM {_table_name(LifeMoment)}")
            )
        ).scalar_one()
    assert rows == 1

    tid = moment_transcript_id(lane=LANE, persona_id="akao", now=_at(14))
    _stored, ver = await load_moment_transcript(tid)
    assert ver == 1
