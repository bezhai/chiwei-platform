"""她读一个别人发给她的文件 —— 读一程、留下印象、下一程接着往后。

四条硬边界：

  * **没有"书"这个注册物。** 她能读的严格等于她手机上收到过的文件，所以"读什么"
    这个问题的答案只能从 ``common_message`` 里来，不从任何书目表来。
  * **认不准是哪个文件就回问，不替她选。** 同一个名字对上好几份（同一本书被两个人
    各发过一次）时，工具把候选连着来路一起摊开让她指，绝不排个序取第一个。
  * **一程读完才落印象，落不下去就什么都不动。** 取不到字节 / 解码失败 / 超时都
    fail-soft：印象和读到第几页原样留在上一程的位置，她可以再读一次。
  * **上限只管她还没翻开过的那些。** 读过的一条都不省 —— 印象是她的记忆，被条数
    上限截掉就是替她遗忘。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import text

from app.data import session as session_mod
from app.living.reading import (
    FILE_LIST_LIMIT,
    READING_TOOLS,
    FilePickedUp,
    FileRead,
    look_for_something_to_read,
    read_a_bit,
    read_a_round,
    read_so_far,
)

LANE = "coe-living"
_CST = dt.timezone(dt.timedelta(hours=8))

_AKAO_BOT_UID = uuid.uuid5(uuid.NAMESPACE_OID, "bot-akao-common-user")
_DM = uuid.uuid5(uuid.NAMESPACE_OID, "conv-dm-chinagi-akao")
_GROUP = uuid.uuid5(uuid.NAMESPACE_OID, "conv-group-lab")
_NOT_HERS = uuid.uuid5(uuid.NAMESPACE_OID, "conv-not-hers")

# 三条消息、三个文件：私聊来的《斜阳.txt》、群里来的**同名**《斜阳.txt》、
# 以及一条她根本不在的会话里的文件（她不该看见它）。
_M_DM_SHAYANG = uuid.uuid5(uuid.NAMESPACE_OID, "msg-dm-shayang")
_M_GROUP_SHAYANG = uuid.uuid5(uuid.NAMESPACE_OID, "msg-group-shayang")
_M_NOT_HERS = uuid.uuid5(uuid.NAMESPACE_OID, "msg-not-hers")


def _at(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 7, 25, hour, minute, tzinfo=_CST)


def _ms(at: dt.datetime) -> int:
    return int(at.timestamp() * 1000)


def _attachment(message_id: uuid.UUID, file_key: str) -> str:
    """附件实例身份 —— 跟实现共用同一个派生函数，不在测试里手写它的格式。"""
    from app.domain.reading_source import derive_attachment_id

    return derive_attachment_id(
        common_message_id=str(message_id), file_key=file_key
    )


async def _someone_sent(
    *,
    message_id: uuid.UUID,
    conversation: uuid.UUID,
    who: str,
    at: dt.datetime,
    file_key: str,
    file_name: str,
) -> None:
    """往会话里落一条"有人发了个文件"的消息。

    形状跟 :data:`_REAL_FILE_ITEM_FROM_LARK` 同源（``kind`` / ``key`` /
    ``meta.file_name``），只是 key 和文件名由用例给。想改这里先去看那份常量。
    """
    content = json.dumps(
        [{"kind": "file", "key": file_key, "meta": {"file_name": file_name}}]
    )
    async with session_mod.get_session() as s:
        await s.execute(
            text(
                "INSERT INTO common_message (common_message_id, channel,"
                " common_conversation_id, sender_display_name, role, content,"
                " scope, event_time)"
                " VALUES (CAST(:m AS uuid), 'lark', CAST(:c AS uuid), :who,"
                " 'user', CAST(:content AS jsonb), 'group', :at)"
            ),
            {
                "m": str(message_id),
                "c": str(conversation),
                "who": who,
                "content": content,
                "at": _ms(at),
            },
        )


@pytest.fixture
async def reading_db(living_db, pinned):
    """她的两条会话 + 一条不是她的会话，外加读到哪了那张表。

    两条会话都摆进她的视野：私聊里有人刚跟她说过话，群固定加白
    （:mod:`app.living.whitelist`）。白名单收窄之后"bot 还在"不再等于"她看得见" ——
    而这个文件里的用例验的是**她读得到的等于她收到过的**，不是白名单本身，所以那道
    闸在这里按住。名单外那条会话里的文件她一个字都看不到，用例在本文件最后一节。
    """
    from tests.runtime.conftest import migrate

    await migrate(FileRead, living_db)

    async with session_mod.get_session() as s:
        await s.execute(
            text(
                "INSERT INTO common_user (common_user_id, channel, display_name)"
                " VALUES (CAST(:u AS uuid), 'lark', '赤尾')"
            ),
            {"u": str(_AKAO_BOT_UID)},
        )
        for conv, scope, title in (
            (_DM, "direct", "千凪"),
            (_GROUP, "group", "宅居研究所"),
            (_NOT_HERS, "group", "别人的群"),
        ):
            await s.execute(
                text(
                    "INSERT INTO common_conversation"
                    " (common_conversation_id, channel, scope, display_name,"
                    " is_active) VALUES (CAST(:c AS uuid), 'lark', :s, :t, true)"
                ),
                {"c": str(conv), "s": scope, "t": title},
            )
        await s.execute(
            text(
                "INSERT INTO bot_config"
                " (bot_name, persona_id, common_user_id, is_active)"
                " VALUES ('chiwei', 'akao', CAST(:u AS uuid), true)"
            ),
            {"u": str(_AKAO_BOT_UID)},
        )
        for conv in (_DM, _GROUP):
            await s.execute(
                text(
                    "INSERT INTO common_bot_presence"
                    " (common_conversation_id, bot_name, is_active)"
                    " VALUES (CAST(:c AS uuid), 'chiwei', true)"
                ),
                {"c": str(conv)},
            )

    pinned(str(_GROUP))
    # 有人在这条私聊里刚说过话 —— 私聊里真人的任意一条就算在叫她。
    async with session_mod.get_session() as s:
        await s.execute(
            text(
                "INSERT INTO common_message (common_message_id, channel,"
                " common_conversation_id, sender_display_name, role, content,"
                " content_text, scope, event_time)"
                " VALUES (CAST(:m AS uuid), 'lark', CAST(:c AS uuid), '千凪',"
                " 'user', CAST(:body AS jsonb), '在吗', 'direct', :at)"
            ),
            {
                "m": str(uuid.uuid5(uuid.NAMESPACE_OID, "msg-dm-hello")),
                "c": str(_DM),
                "body": json.dumps([{"kind": "text", "text": "在吗"}]),
                "at": _ms(_at(21, 0)),
            },
        )
    await _someone_sent(
        message_id=_M_DM_SHAYANG,
        conversation=_DM,
        who="千凪",
        at=_at(20, 10),
        file_key="key-dm-shayang",
        file_name="斜阳.txt",
    )
    await _someone_sent(
        message_id=_M_GROUP_SHAYANG,
        conversation=_GROUP,
        who="绫奈",
        at=_at(20, 20),
        file_key="key-group-shayang",
        file_name="斜阳.txt",
    )
    await _someone_sent(
        message_id=_M_NOT_HERS,
        conversation=_NOT_HERS,
        who="陌生人",
        at=_at(20, 30),
        file_key="key-secret",
        file_name="不该看见.txt",
    )
    return living_db


@pytest.fixture
def picked_up(monkeypatch):
    """接住她拿起文件时吐出去的 durable 触发，不真的进队列。"""
    from app.living import reading as reading_mod

    out: list[FilePickedUp] = []

    async def fake_emit(data):
        out.append(data)

    monkeypatch.setattr(reading_mod, "emit", fake_emit)
    return out


@pytest.fixture
def one_round(monkeypatch):
    """把"读一程"那步换成替身：读到第几页、揉出什么印象，由用例说了算。"""
    from app.living import reading as reading_mod

    class FakeRound:
        def __init__(self) -> None:
            self.calls: list[dict] = []
            self.result = None
            self.before_return = None

        def gives(self, *, impression: str, pages_read: int, finished: bool = False):
            from app.agent.reading import ReadingResult

            self.result = ReadingResult(
                impression=impression, pages_read=pages_read, finished=finished
            )
            return self

        async def __call__(self, **kwargs):
            self.calls.append(kwargs)
            if self.before_return is not None:
                await self.before_return()
            return self.result

    fake = FakeRound()
    monkeypatch.setattr(reading_mod, "run_reading_round", fake)
    return fake


def _trigger(*, attachment_id: str, round_id: str = "r-1", title: str = "斜阳.txt"):
    return FilePickedUp(
        lane=LANE,
        round_id=round_id,
        persona_id="akao",
        attachment_id=attachment_id,
        title=title,
        tos_file="files/key-dm-shayang",
    )


# --------------------------------------------------------------------------
# 一 · 她能读的，严格等于她收到过的
# --------------------------------------------------------------------------


# 飞书投影写进 common_message.content 的文件项 —— **键名和嵌套层次照着库里一条真实
# 记录来**，只把 key 和文件名换成了不含真实信息的等形值（真实的那两个是私人文件，
# 不进仓库）。
#
# **字段名是 kind / key**，不是 type / value。这一条曾经写错过：SQL 和测试数据一起
# 用了臆造的 type/value，两边自洽所以全绿，而线上一个文件都查不出来，她永远只会说
# "没有谁给你发过什么可以读的东西"。这份常量就是防它再漂的锚：要改文件项的形状，
# 先去库里看一条真实记录，别照着实现改。
_REAL_FILE_ITEM_FROM_LARK = {
    "key": "file_v3_00000_00000000-0000-0000-0000-00000000000g",
    "kind": "file",
    "meta": {"file_name": "斜阳.epub", "lark_type": "file"},
}


@pytest.mark.integration
async def test_a_file_in_the_shape_lark_actually_writes_is_found(reading_db):
    """按飞书真实写入的形状落一条文件消息，她必须能查到它。

    只断言"查得到 + 名字对"，不碰任何工具文案 —— 这条测的是查询和真实数据的
    对齐，不是渲染。
    """
    from app.living.reading import files_sent_to

    message_id = uuid.uuid5(uuid.NAMESPACE_OID, "msg-real-shape")
    async with session_mod.get_session() as s:
        await s.execute(
            text(
                "INSERT INTO common_message (common_message_id, channel,"
                " common_conversation_id, sender_display_name, role, content,"
                " scope, event_time)"
                " VALUES (CAST(:m AS uuid), 'lark', CAST(:c AS uuid), '主人',"
                " 'user', CAST(:content AS jsonb), 'direct', :at)"
            ),
            {
                "m": str(message_id),
                "c": str(_DM),
                "content": json.dumps([_REAL_FILE_ITEM_FROM_LARK]),
                "at": _ms(_at(21, 40)),
            },
        )

    found = await files_sent_to(persona_id="akao", now=_at(21, 30))

    assert [f.title for f in found if f.title == "斜阳.epub"] == [
        "斜阳.epub"
    ], f"飞书真实形状的文件项没被查出来，查到的是 {[f.title for f in found]}"


async def _taken_back(message_id: uuid.UUID, *, at: dt.datetime) -> None:
    """渠道那边把这条消息撤掉了。

    撤回**不删**公共层那一行（那是消息记录，删行会打断历史），只在 ``recalled_at``
    上留个时刻。所以"这份东西还拿不拿得到"由这一列说了算。
    """
    async with session_mod.get_session() as s:
        await s.execute(
            text(
                "UPDATE common_message SET recalled_at = :at"
                " WHERE common_message_id = CAST(:m AS uuid)"
            ),
            {"at": at, "m": str(message_id)},
        )


@pytest.mark.integration
async def test_a_file_she_never_opened_and_was_taken_back_is_gone_for_her(
    reading_db, in_a_moment, picked_up
):
    """她从没翻开过、又被撤回的那份：清单上没有它，报名字也拿不起来。

    她从没打开过它，撤掉了就等于没来过 —— 没有任何印象要保住，摆在清单上只是给她
    一个指了会失败的东西。

    判据写在"这一行在渠道上还在不在"上，不写在谁撤的它上面：她自己撤的和别人撤的是
    同一件事，取字节那一侧没有理由分开对待。
    """
    message_id = uuid.uuid5(uuid.NAMESPACE_OID, "msg-dm-taken-back")
    await _someone_sent(
        message_id=message_id,
        conversation=_DM,
        who="主人",
        at=_at(22, 10),
        file_key="file-key-taken-back",
        file_name="撤回了的那本.epub",
    )
    await _taken_back(message_id, at=_at(22, 20))

    async with in_a_moment("akao"):
        listed = await look_for_something_to_read.invoke({})
        outcome = await read_a_bit.invoke({"which": "撤回了的那本"})

    assert "撤回了的那本.epub" not in listed, (
        f"她从没翻开过、又被撤掉的那份还列在她眼前：{listed}"
    )
    assert isinstance(outcome, dict), "她拿起了一个已经撤回的文件"
    assert "拿不到" in outcome["message"], (
        f"该如实说这份东西已经不在了，而不是说没有对得上的。拿到：{outcome['message']}"
    )
    assert picked_up == []


@pytest.mark.integration
async def test_a_file_taken_back_keeps_what_she_read_but_offers_no_handle(
    reading_db, in_a_moment, one_round, picked_up
):
    """有人撤回一份她**读过**的文件：印象和读到第几页照常在她眼前，只是拿不到了。

    撤回改变的是"现在还能不能拿到"，不是"有没有发生过"。她确实读了那 12 页、确实
    留下了那句印象 —— 连着从她眼前抹掉就是替她遗忘一件真发生过的事。真人也一样：
    对方撤回一份文件，你打不开它了，但你读过它这件事和读书笔记都还在。

    **但不给可执行的 ``file=`` 句柄**，并写明这份东西已经拿不到了：留着句柄等于同时
    说"这个拿不到了"和"拿这串去读它"，她照着指一次只会被顶回来。
    """
    dm_one = _attachment(_M_DM_SHAYANG, "key-dm-shayang")
    one_round.gives(impression="太宰那个调子，我读着有点上头。", pages_read=12)
    await read_a_round(
        _trigger(attachment_id=dm_one, round_id="r-1", title="斜阳.txt")
    )
    await _taken_back(_M_DM_SHAYANG, at=_at(22, 30))

    async with in_a_moment("akao"):
        listed = await look_for_something_to_read.invoke({})
        outcome = await read_a_bit.invoke({"which": dm_one})

    assert "太宰那个调子，我读着有点上头。" in listed, (
        f"撤回把她读过的那份印象一起从她眼前抹掉了。拿到：\n{listed}"
    )
    assert "你已经读了 12 页" in listed, f"她读到哪了没告诉她。拿到：\n{listed}"
    assert "拿不到" in listed, (
        f"没写明这份东西已经拿不到了 —— 她会以为还能接着读。拿到：\n{listed}"
    )
    assert dm_one not in listed, (
        f"撤回掉的那份还挂着可执行的句柄，她照它指一次只会被顶回来。拿到：\n{listed}"
    )
    assert isinstance(outcome, dict), "她拿起了一个已经被撤回的文件"
    assert "拿不到" in outcome["message"], (
        f"该如实说这份东西已经不在了。拿到：{outcome['message']}"
    )
    assert picked_up == []


@pytest.mark.integration
async def test_she_can_only_pick_up_files_from_conversations_that_are_hers(
    reading_db, in_a_moment, picked_up
):
    """不在她手机上的那条会话里的文件，她连名字都看不到，更拿不起来。"""
    async with in_a_moment("akao"):
        listed = await look_for_something_to_read.invoke({})
        outcome = await read_a_bit.invoke({"which": "不该看见"})

    assert "不该看见" not in listed
    assert "斜阳.txt" in listed
    assert isinstance(outcome, dict), "她拿起了一个不在她手机上的文件"
    assert picked_up == []


@pytest.mark.integration
async def test_picking_up_a_file_by_name_starts_exactly_one_round(
    reading_db, in_a_moment, picked_up
):
    """报一个名字、正好对上一份 → 拿起来读一程，触发带着这个附件实例的身份。"""
    async with in_a_moment("akao"):
        said = await read_a_bit.invoke({"which": "人间失格"})

    assert isinstance(said, dict), "还没有这个文件却开读了"

    await _someone_sent(
        message_id=uuid.uuid5(uuid.NAMESPACE_OID, "msg-dm-ningen"),
        conversation=_DM,
        who="千凪",
        at=_at(21, 0),
        file_key="key-ningen",
        file_name="人间失格.epub",
    )
    async with in_a_moment("akao"):
        said = await read_a_bit.invoke({"which": "人间失格"})

    assert isinstance(said, str), said
    assert len(picked_up) == 1
    only = picked_up[0]
    assert only.lane == LANE and only.persona_id == "akao"
    assert only.title == "人间失格.epub"
    assert only.attachment_id == _attachment(
        uuid.uuid5(uuid.NAMESPACE_OID, "msg-dm-ningen"), "key-ningen"
    )
    assert only.tos_file == "files/key-ningen"


@pytest.mark.integration
async def test_a_name_that_matches_nothing_is_said_so_and_starts_nothing(
    reading_db, in_a_moment, picked_up
):
    """没有对得上的就如实说没有 —— 不拿一个别的文件糊弄过去。"""
    async with in_a_moment("akao"):
        outcome = await read_a_bit.invoke({"which": "百年孤独"})

    assert isinstance(outcome, dict)
    assert "百年孤独" in outcome["message"]
    assert picked_up == []


@pytest.mark.integration
async def test_two_files_with_the_same_name_are_handed_back_for_her_to_pick(
    reading_db, in_a_moment, picked_up
):
    """同名多份 → 把来路摊开让她自己指，绝不替她挑一个。

    "让她指"要真的指得动：回问里给的那串 file=... 再报一次就能开读，而且开的是
    她指的那一份。只列名字不给可指之物的话，这条回问就是死路。
    """
    dm_one = _attachment(_M_DM_SHAYANG, "key-dm-shayang")
    group_one = _attachment(_M_GROUP_SHAYANG, "key-group-shayang")

    async with in_a_moment("akao"):
        outcome = await read_a_bit.invoke({"which": "斜阳"})

    assert isinstance(outcome, dict), "同名两份，她被替着选了一个"
    assert picked_up == []
    message = outcome["message"]
    assert dm_one in message and group_one in message
    # 光有两串 id 分不出哪个是哪个 —— 来路（谁发的）必须在回问里。
    assert "千凪" in message and "绫奈" in message

    async with in_a_moment("akao"):
        said = await read_a_bit.invoke({"which": group_one})

    assert isinstance(said, str), said
    assert [p.attachment_id for p in picked_up] == [group_one]


@pytest.mark.integration
async def test_she_can_point_back_with_the_handle_exactly_as_it_was_shown(
    reading_db, in_a_moment, picked_up
):
    """回问里怎么写的，她照抄回来就得认。

    实测她照抄的是整串 ``file=<id>``（回问正文里就是这么印的），而不是剥掉前缀
    的裸 id —— 只认裸 id 的话这条回问是死路：摊开候选、她指了、被告知"没有名字
    对得上"。所以这里不手搓 id，直接从回问正文里把她会看到的那一串抠出来。
    """
    import re

    async with in_a_moment("akao"):
        outcome = await read_a_bit.invoke({"which": "斜阳"})

    assert isinstance(outcome, dict), "同名两份，她被替着选了一个"
    # 正文末尾那句提示里也有一个占位的 "file=…"，它不是可指之物。按**真实身份的形状**
    # 排（``derive_attachment_id`` 派生的是 ``<消息 id>:<file_key>``，必带冒号），
    # 不按占位符怎么写来排 —— 后者改一次措辞这条测试就失灵。
    handles = [h for h in re.findall(r"file=\S+", outcome["message"]) if ":" in h]
    assert len(handles) == 2, f"回问里没给出可指之物：{outcome['message']}"

    async with in_a_moment("akao"):
        said = await read_a_bit.invoke({"which": handles[1]})

    assert isinstance(said, str), said
    assert [p.attachment_id for p in picked_up] == [
        handles[1].removeprefix("file=")
    ]


# --------------------------------------------------------------------------
# 二 · 同一缝里拿起同一个文件，只读一程
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_picking_up_the_same_file_twice_in_one_moment_is_one_round(
    reading_db, in_a_moment, picked_up
):
    """一缝里重复拿起（工具重试、整轮重放）派生同一个 round_id，下一缝才是新的一程。"""
    dm_one = _attachment(_M_DM_SHAYANG, "key-dm-shayang")

    async with in_a_moment("akao", moment_id="2026-07-25T21:30+08:00"):
        await read_a_bit.invoke({"which": dm_one})
        await read_a_bit.invoke({"which": dm_one})
    async with in_a_moment("akao", moment_id="2026-07-25T21:40+08:00"):
        await read_a_bit.invoke({"which": dm_one})

    ids = [p.round_id for p in picked_up]
    assert len(ids) == 3
    assert ids[0] == ids[1], "同一缝里拿起两次派生了两程 —— 一程会被白读两遍"
    assert ids[2] != ids[0], "下一缝拿不起同一个文件了 —— 她再也读不下去"


# --------------------------------------------------------------------------
# 三 · 一程读完，留下读到哪 + 印象
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_round_leaves_where_she_got_to_and_what_it_left_her_with(
    reading_db, one_round
):
    one_round.gives(impression="直子那段让我心里发紧。", pages_read=7)

    await read_a_round(_trigger(attachment_id="msg-1:key-a", round_id="r-1"))

    mark = await read_so_far(
        lane=LANE, persona_id="akao", attachment_id="msg-1:key-a"
    )
    assert mark is not None
    assert mark.impression == "直子那段让我心里发紧。"
    assert mark.pages_read == 7
    assert mark.finished is False
    assert mark.round_id == "r-1"
    assert mark.title == "斜阳.txt"


@pytest.mark.integration
async def test_the_next_round_picks_up_where_the_last_one_stopped(
    reading_db, one_round
):
    """连读两程：第二程拿到的是上一程的前沿和上一程的印象，不是从头再来。"""
    one_round.gives(impression="刚翻开，还没什么感觉。", pages_read=7)
    await read_a_round(_trigger(attachment_id="msg-1:key-a", round_id="r-1"))

    one_round.gives(impression="读到后面，那种沉下去的劲儿上来了。", pages_read=19)
    await read_a_round(_trigger(attachment_id="msg-1:key-a", round_id="r-2"))

    assert one_round.calls[1]["start_page"] == 7
    assert one_round.calls[1]["prior_impression"] == "刚翻开，还没什么感觉。"

    mark = await read_so_far(
        lane=LANE, persona_id="akao", attachment_id="msg-1:key-a"
    )
    assert (mark.pages_read, mark.ver) == (19, 2)
    assert mark.impression == "读到后面，那种沉下去的劲儿上来了。"


@pytest.mark.integration
async def test_the_first_round_starts_at_the_beginning_with_no_prior_impression(
    reading_db, one_round
):
    one_round.gives(impression="翻开了。", pages_read=3)

    await read_a_round(_trigger(attachment_id="msg-1:key-a"))

    assert one_round.calls[0]["start_page"] == 0
    assert one_round.calls[0]["prior_impression"] is None


@pytest.mark.integration
async def test_reading_to_the_end_is_recorded_as_such(reading_db, one_round):
    one_round.gives(impression="读完了，最后一页停了很久。", pages_read=42, finished=True)

    await read_a_round(_trigger(attachment_id="msg-1:key-a"))

    mark = await read_so_far(
        lane=LANE, persona_id="akao", attachment_id="msg-1:key-a"
    )
    assert mark.finished is True


# --------------------------------------------------------------------------
# 四 · 读不成的时候，印象一个字都不动
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_round_that_fails_leaves_the_impression_where_it_was(
    reading_db, one_round
):
    one_round.gives(impression="第一程留下的。", pages_read=7)
    await read_a_round(_trigger(attachment_id="msg-1:key-a", round_id="r-1"))

    one_round.result = None  # 这一程没读成（超时 / 空产出 / 取不到字节）
    await read_a_round(_trigger(attachment_id="msg-1:key-a", round_id="r-2"))

    mark = await read_so_far(
        lane=LANE, persona_id="akao", attachment_id="msg-1:key-a"
    )
    assert (mark.impression, mark.pages_read, mark.ver) == ("第一程留下的。", 7, 1)
    assert mark.round_id == "r-1"


@pytest.mark.integration
async def test_bytes_that_never_arrive_do_not_write_half_an_impression(
    reading_db, monkeypatch
):
    """取件失败走的是真的那条阅读一程 —— 取不到字节就整程作废，什么都不落。"""
    from app.agent import reading as agent_reading

    asked: list[str] = []

    async def no_bytes(*, tos_file):
        asked.append(tos_file)
        return None

    monkeypatch.setattr(agent_reading, "fetch_attachment_bytes", no_bytes)

    await read_a_round(_trigger(attachment_id="msg-1:key-a"))

    # 真的走到取字节那一步了才算数：这一步没被调到的话，下面那个"什么都没落"就
    # 可能是别的原因造成的，测不出 fail-soft。
    assert asked == ["files/key-dm-shayang"]
    assert (
        await read_so_far(
            lane=LANE, persona_id="akao", attachment_id="msg-1:key-a"
        )
        is None
    )


# --------------------------------------------------------------------------
# 五 · 同一程递两次，不读两遍
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_the_same_round_delivered_twice_does_not_read_twice(
    reading_db, one_round
):
    """durable 重投 / 租约过期被接手：昂贵的那一程不能重跑，页号更不能双推进。"""
    one_round.gives(impression="读了一程。", pages_read=7)

    await read_a_round(_trigger(attachment_id="msg-1:key-a", round_id="r-1"))
    await read_a_round(_trigger(attachment_id="msg-1:key-a", round_id="r-1"))

    assert len(one_round.calls) == 1, "同一程被读了两遍 —— 白烧一次钱"
    mark = await read_so_far(
        lane=LANE, persona_id="akao", attachment_id="msg-1:key-a"
    )
    assert (mark.pages_read, mark.ver) == (7, 1)


@pytest.mark.integration
async def test_a_stale_round_never_overwrites_a_newer_impression(
    reading_db, one_round
):
    """拿着过时印象的那一程（并发 / 重放）提交会被拒，新的那版原样留着。"""
    from app.runtime.persist import insert_append

    async def someone_else_got_there_first():
        await insert_append(
            FileRead(
                lane=LANE,
                persona_id="akao",
                attachment_id="msg-1:key-a",
                ver=0,
                title="斜阳.txt",
                impression="别的那一程写下的。",
                pages_read=30,
                finished=False,
                read_at=_at(22),
                round_id="r-other",
            ),
            expected_current_ver=0,
        )

    one_round.gives(impression="过时那一程写的。", pages_read=7)
    one_round.before_return = someone_else_got_there_first

    await read_a_round(_trigger(attachment_id="msg-1:key-a", round_id="r-1"))

    mark = await read_so_far(
        lane=LANE, persona_id="akao", attachment_id="msg-1:key-a"
    )
    assert mark.impression == "别的那一程写下的。"
    assert (mark.pages_read, mark.ver) == (30, 1)


# --------------------------------------------------------------------------
# 六 · 她问得到自己读到哪、记得什么
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_she_gets_her_own_impression_back_when_she_asks(
    reading_db, in_a_moment, one_round
):
    """印象不是写给系统看的：她问「有什么能读的」，读过的那份原样回到她眼前。"""
    dm_one = _attachment(_M_DM_SHAYANG, "key-dm-shayang")
    one_round.gives(impression="太宰那个调子，我读着有点上头。", pages_read=12)
    await read_a_round(
        _trigger(attachment_id=dm_one, round_id="r-1", title="斜阳.txt")
    )

    async with in_a_moment("akao"):
        listed = await look_for_something_to_read.invoke({})

    assert "太宰那个调子，我读着有点上头。" in listed
    assert "12" in listed, "她读到哪了没告诉她"
    assert dm_one in listed


@pytest.mark.integration
async def test_nothing_to_read_is_said_plainly(living_db, in_a_moment):
    from tests.runtime.conftest import migrate

    await migrate(FileRead, living_db)
    async with in_a_moment("akao"):
        listed = await look_for_something_to_read.invoke({})

    assert "没有谁给你发过" in listed, listed


@pytest.mark.integration
async def test_the_list_caps_the_unopened_pile_but_never_her_own_memory(
    reading_db, in_a_moment, one_round
):
    """条数上限只管她还没翻开过的那摞。

    读过的一条都不省 —— 印象是她的记忆，被条数上限截掉就是替她遗忘。而没翻开过的
    那摞被挤下去也不等于永久丢失：报名字那条路一个上限都没有，老的那个照样指得到。
    """
    oldest_key = "key-oldest"
    oldest_message = uuid.uuid5(uuid.NAMESPACE_OID, "msg-oldest")
    await _someone_sent(
        message_id=oldest_message,
        conversation=_DM,
        who="千凪",
        at=_at(6, 0),
        file_key=oldest_key,
        file_name="很久以前那本.txt",
    )
    for i in range(FILE_LIST_LIMIT + 2):
        await _someone_sent(
            message_id=uuid.uuid5(uuid.NAMESPACE_OID, f"msg-filler-{i}"),
            conversation=_DM,
            who="千凪",
            at=_at(22, i),
            file_key=f"key-filler-{i}",
            file_name=f"填充{i}.txt",
        )

    read_long_ago = _attachment(oldest_message, oldest_key)
    one_round.gives(impression="很久以前读的，还记得那个开头。", pages_read=5)
    await read_a_round(
        _trigger(
            attachment_id=read_long_ago, round_id="r-old", title="很久以前那本.txt"
        )
    )

    async with in_a_moment("akao"):
        listed = await look_for_something_to_read.invoke({})
        picked = await read_a_bit.invoke({"which": "很久以前那本"})

    # 上限真的咬到了没翻开的那摞（最老那几个填充被挤下去），而她读过的那本还在。
    assert "填充0.txt" not in listed and "填充1.txt" not in listed, listed
    assert "很久以前读的，还记得那个开头。" in listed, (
        "她读过的那本被条数上限挤掉了 —— 印象被截掉就是替她遗忘"
    )
    assert isinstance(picked, str), picked


# --------------------------------------------------------------------------
# 七 · 结构性的两条：不问时长、不开入口
# --------------------------------------------------------------------------


def test_no_reading_tool_ever_asks_her_how_long_something_takes():
    """真人对"多久"没有内感受。问她要一个分钟数就是把生活切成日程表。"""
    banned = ("minute", "duration", "how_long", "seconds", "until", "hour")
    for t in READING_TOOLS:
        params = t.definition.parameters.get("properties", {})
        for pname in params:
            assert not any(b in pname.lower() for b in banned), (
                f"{t.name} 的参数 {pname} 在问她一个时长 —— 她读的是一程，不是一段时间"
            )


def _in_a_fresh_process(expr: str, *, lane: str) -> str:
    env = dict(os.environ)
    env["LANE"] = lane
    proc = subprocess.run(
        [sys.executable, "-c", f"import app.wiring;{expr}"],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_the_reading_round_is_a_durable_edge_and_not_an_inbound_one():
    """读一程那条 durable 边**不产生任何 source** —— 它不是给外面的消息开的口。

    ``.durable()`` 只是 ``WireBuilder`` 上的一个标志位（见 ``app/runtime/wire.py``），
    它不往 ``WireSpec.sources`` 里放东西，所以 ``tests/living/test_no_inbound.py``
    那条"实验泳道上一条 mq / http 源都没有"照旧成立。这条边上跑的东西只有她自己
    刚刚在某一缝里拿起的那个文件，投递方和消费方都是这一个进程。

    同时钉住这条边在每条泳道上都挂着：挂边跟泳道名无关（那道按泳道名分流的门随
    旧实现一起删掉了）。
    """
    pairs = (
        "from app.runtime.wire import WIRING_REGISTRY;"
        "print(sorted((s.data_type.__name__, c.__name__)"
        " for s in WIRING_REGISTRY for c in s.consumers));"
        "print(sorted((s.data_type.__name__, x.kind)"
        " for s in WIRING_REGISTRY for x in s.sources))"
    )
    on_lane, off_lane = (
        _in_a_fresh_process(pairs, lane=LANE),
        _in_a_fresh_process(pairs, lane="prod"),
    )
    wired, sources = on_lane.strip().splitlines()
    assert "('FilePickedUp', 'read_a_round')" in wired, wired
    assert "FilePickedUp" not in sources, (
        f"读一程那条边挂上了 source —— 那就是个信箱了。拿到：{sources}"
    )
    assert "('FilePickedUp', 'read_a_round')" in off_lane, (
        f"读一程在别的泳道上不见了 —— 挂边不该看泳道名。拿到：{off_lane}"
    )


def test_both_hands_are_ones_she_actually_has():
    """这两只手要真在她那一缝的工具集里。

    没挂上去是**静默失败**：模块写好了、这个文件里的用例全绿，durable 边也接上了，
    但她那一缝的工具列表里没有它们，于是永远不会调、那条边永远不会被触发。同款用例
    见 ``test_phone.py`` 的 ``test_finding_someone_is_one_of_the_hands_she_actually_has``。
    """
    from app.living.moment import MOMENT_TOOLS

    assert look_for_something_to_read in MOMENT_TOOLS, "她手里没有找东西读这只手"
    assert read_a_bit in MOMENT_TOOLS, "她手里没有往下读一程这只手"


# --------------------------------------------------------------------------
# 八 · 会话白名单：她读得到的严格等于她看得见的
# --------------------------------------------------------------------------
#
# 这两条钉的是收敛之前那个口子：找可读文件的查询自己内联一份会话集合，所以主闸落在
# 可达性上也管不到它 —— 一条掉出名单的会话里的文件仍然会连着文件名、发件人、会话
# 标题一起摆到她眼前，拿起来还能读全文。


async def _a_quiet_group_with_a_file() -> uuid.UUID:
    """一个 bot 还在、但没人叫过她的群，里面有人发了个文件。"""
    quiet = uuid.uuid5(uuid.NAMESPACE_OID, "conv-group-nobody-calls-her")
    async with session_mod.get_session() as s:
        await s.execute(
            text(
                "INSERT INTO common_conversation"
                " (common_conversation_id, channel, scope, display_name, is_active)"
                " VALUES (CAST(:c AS uuid), 'lark', 'group', '没人叫她的群', true)"
            ),
            {"c": str(quiet)},
        )
        await s.execute(
            text(
                "INSERT INTO common_bot_presence"
                " (common_conversation_id, bot_name, is_active)"
                " VALUES (CAST(:c AS uuid), 'chiwei', true)"
            ),
            {"c": str(quiet)},
        )
    await _someone_sent(
        message_id=uuid.uuid5(uuid.NAMESPACE_OID, "msg-quiet-group-file"),
        conversation=quiet,
        who="陌生人",
        at=_at(20, 40),
        file_key="key-quiet-group",
        file_name="名单外的那本.txt",
    )
    return quiet


@pytest.mark.integration
async def test_a_file_from_a_conversation_out_of_sight_is_never_listed(
    reading_db, in_a_moment
):
    await _a_quiet_group_with_a_file()

    async with in_a_moment("akao"):
        listed = await look_for_something_to_read.invoke({})

    assert "名单外的那本" not in listed, (
        f"名单外那条会话里的文件摆到了她眼前 —— 连着文件名、谁发的、发在哪个群。"
        f"拿到：\n{listed}"
    )


@pytest.mark.integration
async def test_she_cannot_pick_up_a_file_from_a_conversation_out_of_sight(
    reading_db, in_a_moment, picked_up
):
    await _a_quiet_group_with_a_file()

    async with in_a_moment("akao"):
        outcome = await read_a_bit.invoke({"which": "名单外的那本"})

    assert isinstance(outcome, dict), "她读起了名单外那条会话里的文件"
    assert picked_up == []
