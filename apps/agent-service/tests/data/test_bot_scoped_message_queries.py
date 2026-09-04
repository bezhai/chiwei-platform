"""公共层里"某个 bot 身份看得到什么"的那批查询（真 Postgres 集成测试）。

这批查询锚在 ``bot_config.persona_id`` 上：一个 persona 名下挂着若干个 bot
（正式那个和 dev 那个指向同一个人），"她能看到哪些会话""哪条消息是她自己说的"
"谁点了她的名"全部由这层映射决定，``role`` 一个字都答不出来（同一个群里的几个
bot 出站全是 ``role='assistant'``）。

``bot_config`` / ``common_bot_presence`` 由 channel-server 管理、不在
agent-service 的 SQLAlchemy 模型里（只读裸表），所以这里手建，只建查询真的用到
的列 —— 跟 ``tests/living/conftest.py`` 那份 DDL 同源。
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import text

import app.data.session as session_mod
from app.data.models import Base, CommonConversation, CommonMessage
from app.data.queries.messages import (
    find_conversation_window,
    find_file_items_in_persona_conversations,
    find_messages_by_outbound_ids,
    find_messages_known_through,
    find_newest_unread_summons,
    find_recall_state_by_outbound_ids,
    find_unread_senders,
    find_unread_summary,
    search_persona_conversations_by_name,
)
from app.data.queries.persona import (
    find_bot_names_for_persona,
    find_bot_user_ids_for_persona,
    find_conversations_with_persona_bot,
)

_CST = dt.timezone(dt.timedelta(hours=8))

_AKAO_BOT_UID = uuid.uuid5(uuid.NAMESPACE_OID, "q-bot-akao")
_AYANA_BOT_UID = uuid.uuid5(uuid.NAMESPACE_OID, "q-bot-ayana")
_HUMAN = uuid.uuid5(uuid.NAMESPACE_OID, "q-human")

_DM = uuid.uuid5(uuid.NAMESPACE_OID, "q-conv-dm")
_GROUP = uuid.uuid5(uuid.NAMESPACE_OID, "q-conv-group")
_NOT_HERS = uuid.uuid5(uuid.NAMESPACE_OID, "q-conv-not-hers")

_BOT_CONFIG_DDL = (
    "CREATE TABLE bot_config ("
    "  bot_name VARCHAR(50) PRIMARY KEY,"
    "  persona_id VARCHAR(50),"
    "  common_user_id UUID,"
    "  is_active BOOLEAN NOT NULL DEFAULT TRUE"
    ")"
)
_BOT_PRESENCE_DDL = (
    "CREATE TABLE common_bot_presence ("
    "  common_conversation_id UUID NOT NULL,"
    "  bot_name VARCHAR(50) NOT NULL,"
    "  is_active BOOLEAN NOT NULL DEFAULT TRUE,"
    "  PRIMARY KEY (common_conversation_id, bot_name)"
    ")"
)


def _ms(moment: dt.datetime) -> int:
    return int(moment.timestamp() * 1000)


def _at(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 7, 25, hour, minute, tzinfo=_CST)


@pytest.fixture
async def bot_db(test_db):
    """公共层三张 ORM 表 + channel-server 那两张裸表。"""
    tables = [
        CommonConversation.__table__,
        CommonMessage.__table__,
    ]
    async with test_db.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables)
        )
        await conn.execute(text(_BOT_CONFIG_DDL))
        await conn.execute(text(_BOT_PRESENCE_DDL))
    yield test_db


async def _conversation(
    conv: uuid.UUID, *, scope: str, title: str | None, is_active: bool = True
) -> None:
    async with session_mod.get_session() as s:
        await s.execute(
            text(
                "INSERT INTO common_conversation "
                "(common_conversation_id, channel, scope, display_name, is_active) "
                "VALUES (CAST(:c AS uuid), 'lark', :sc, :t, :a)"
            ),
            {"c": str(conv), "sc": scope, "t": title, "a": is_active},
        )


async def _bot(
    bot_name: str,
    persona_id: str,
    *,
    common_user_id: uuid.UUID | None = None,
    is_active: bool = True,
) -> None:
    async with session_mod.get_session() as s:
        await s.execute(
            text(
                "INSERT INTO bot_config "
                "(bot_name, persona_id, common_user_id, is_active) "
                "VALUES (:b, :p, CAST(:u AS uuid), :a)"
            ),
            {
                "b": bot_name,
                "p": persona_id,
                "u": str(common_user_id) if common_user_id else None,
                "a": is_active,
            },
        )


async def _present(conv: uuid.UUID, bot_name: str, *, is_active: bool = True) -> None:
    async with session_mod.get_session() as s:
        await s.execute(
            text(
                "INSERT INTO common_bot_presence "
                "(common_conversation_id, bot_name, is_active) "
                "VALUES (CAST(:c AS uuid), :b, :a)"
            ),
            {"c": str(conv), "b": bot_name, "a": is_active},
        )


async def _message(
    conv: uuid.UUID,
    *,
    at: dt.datetime,
    role: str = "user",
    who: str | None = "路人",
    body: str = "在吗",
    content: list[dict] | None = None,
    bot_name: str | None = None,
    mentions: list[uuid.UUID] | None = None,
    recalled_at: dt.datetime | None = None,
    outbound_id: uuid.UUID | None = None,
    message_id: uuid.UUID | None = None,
    scope: str = "direct",
) -> uuid.UUID:
    mid = message_id or uuid.uuid4()
    import json

    async with session_mod.get_session() as s:
        await s.execute(
            text(
                "INSERT INTO common_message "
                "(common_message_id, channel, common_conversation_id, "
                " common_user_id, sender_display_name, role, content, "
                " content_text, mentioned_common_user_ids, agent_outbound_id, "
                " recalled_at, scope, bot_name, event_time) "
                "VALUES (CAST(:mid AS uuid), 'lark', CAST(:c AS uuid), "
                " CAST(:u AS uuid), :who, :role, CAST(:content AS jsonb), "
                " :ct, CAST(:men AS uuid[]), CAST(:oid AS uuid), "
                " :rec, :scope, :bn, :et)"
            ),
            {
                "mid": str(mid),
                "c": str(conv),
                "u": str(_HUMAN) if role == "user" else None,
                "who": who,
                "role": role,
                "content": json.dumps(
                    content
                    if content is not None
                    else [{"kind": "text", "text": body}]
                ),
                "ct": body,
                "men": [str(m) for m in mentions] if mentions is not None else None,
                "oid": str(outbound_id) if outbound_id else None,
                "rec": recalled_at,
                "scope": scope,
                "bn": bot_name,
                "et": _ms(at),
            },
        )
    return mid


async def _seed_her_phone() -> None:
    """一个 persona、两个 bot、一条私聊 + 一个群 + 一条不属于她的会话。"""
    await _bot("chiwei", "akao", common_user_id=_AKAO_BOT_UID)
    await _bot("chiwei-dev", "akao", common_user_id=None)
    await _bot("ayana", "ayana", common_user_id=_AYANA_BOT_UID)
    await _conversation(_DM, scope="direct", title=None)
    await _conversation(_GROUP, scope="group", title="实验室")
    await _conversation(_NOT_HERS, scope="group", title="别人的群")
    await _present(_DM, "chiwei")
    await _present(_GROUP, "chiwei")
    await _present(_GROUP, "ayana")
    await _present(_NOT_HERS, "ayana")


# ---------------------------------------------------------------------------
# bot_config：她的 bot 是哪些、它们在公共层的身份是什么
# ---------------------------------------------------------------------------


async def test_her_bot_names_come_from_bot_config(bot_db):
    await _seed_her_phone()
    assert sorted(await find_bot_names_for_persona("akao")) == [
        "chiwei",
        "chiwei-dev",
    ]


async def test_inactive_bots_are_not_hers(bot_db):
    await _seed_her_phone()
    await _bot("chiwei-old", "akao", is_active=False)
    assert "chiwei-old" not in await find_bot_names_for_persona("akao")


async def test_bot_user_ids_skip_the_bots_that_never_backfilled_one(bot_db):
    """``common_user_id`` 没回填的 bot 不进结果 —— 那一列是"群里点的是不是她"的
    全部依据，NULL 混进去就会拿一个 ``None`` 去跟 mention 数组比。"""
    await _seed_her_phone()
    assert await find_bot_user_ids_for_persona("akao") == [str(_AKAO_BOT_UID)]


async def test_bot_user_ids_empty_for_unknown_persona(bot_db):
    await _seed_her_phone()
    assert await find_bot_user_ids_for_persona("nobody") == []


# ---------------------------------------------------------------------------
# common_bot_presence：她手机上有哪些会话
# ---------------------------------------------------------------------------


async def test_conversations_are_the_ones_her_bot_is_still_in(bot_db):
    await _seed_her_phone()
    rows = await find_conversations_with_persona_bot("akao")
    assert {str(r["channel_id"]) for r in rows} == {str(_DM), str(_GROUP)}


async def test_each_conversation_carries_the_bot_identity_to_speak_with(bot_db):
    await _seed_her_phone()
    rows = await find_conversations_with_persona_bot("akao")
    by_id = {str(r["channel_id"]): r for r in rows}
    assert by_id[str(_DM)]["bot_name"] == "chiwei"
    assert by_id[str(_DM)]["scope"] == "direct"
    assert by_id[str(_DM)]["channel"] == "lark"
    assert by_id[str(_GROUP)]["title"] == "实验室"


async def test_missing_conversation_title_reads_as_empty_not_null(bot_db):
    await _seed_her_phone()
    rows = await find_conversations_with_persona_bot("akao")
    by_id = {str(r["channel_id"]): r for r in rows}
    assert by_id[str(_DM)]["title"] == ""


async def test_a_bot_removed_from_the_group_takes_the_conversation_away(bot_db):
    await _seed_her_phone()
    async with session_mod.get_session() as s:
        await s.execute(
            text(
                "UPDATE common_bot_presence SET is_active = false "
                "WHERE common_conversation_id = CAST(:c AS uuid)"
            ),
            {"c": str(_GROUP)},
        )
    rows = await find_conversations_with_persona_bot("akao")
    assert {str(r["channel_id"]) for r in rows} == {str(_DM)}


async def test_an_archived_conversation_is_not_on_her_phone(bot_db):
    await _seed_her_phone()
    async with session_mod.get_session() as s:
        await s.execute(
            text(
                "UPDATE common_conversation SET is_active = false "
                "WHERE common_conversation_id = CAST(:c AS uuid)"
            ),
            {"c": str(_DM)},
        )
    rows = await find_conversations_with_persona_bot("akao")
    assert {str(r["channel_id"]) for r in rows} == {str(_GROUP)}


# ---------------------------------------------------------------------------
# 未读：信封那一侧
# ---------------------------------------------------------------------------


def _unread_args(conv: uuid.UUID, *, after_ms: int = 0, after_id: str = "") -> dict:
    return {
        "channel_id": str(conv),
        "after_ms": after_ms,
        "after_id": after_id,
        "own_bots": ["chiwei", "chiwei-dev"],
    }


async def test_unread_summary_counts_only_what_she_has_not_seen(bot_db):
    await _seed_her_phone()
    await _message(_DM, at=_at(9))
    await _message(_DM, at=_at(10))
    row = await find_unread_summary(
        **_unread_args(_DM), bot_user_ids=[str(_AKAO_BOT_UID)]
    )
    assert row["unread"] == 2
    assert row["earliest"] == _ms(_at(9))
    assert row["latest"] == _ms(_at(10))


async def test_her_own_lines_are_never_unread(bot_db):
    await _seed_her_phone()
    await _message(_DM, at=_at(9), role="assistant", bot_name="chiwei", who="赤尾")
    row = await find_unread_summary(
        **_unread_args(_DM), bot_user_ids=[str(_AKAO_BOT_UID)]
    )
    assert row["unread"] == 0


async def test_a_sisters_line_in_the_same_group_is_unread(bot_db):
    """同一个群里姐姐也是 ``role='assistant'`` —— 认 ``bot_name`` 才分得开。"""
    await _seed_her_phone()
    await _message(
        _GROUP, at=_at(9), role="assistant", bot_name="ayana", who="绫奈", scope="group"
    )
    row = await find_unread_summary(
        **_unread_args(_GROUP), bot_user_ids=[str(_AKAO_BOT_UID)]
    )
    assert row["unread"] == 1


async def test_a_recalled_line_is_out_of_the_conversation(bot_db):
    await _seed_her_phone()
    await _message(_DM, at=_at(9), recalled_at=_at(9, 5))
    row = await find_unread_summary(
        **_unread_args(_DM), bot_user_ids=[str(_AKAO_BOT_UID)]
    )
    assert row["unread"] == 0


async def test_the_cursor_is_composite_so_the_same_millisecond_is_not_skipped(bot_db):
    """只按 ``event_time >`` 开窗会把整个那一毫秒排除掉。"""
    await _seed_her_phone()
    a = uuid.UUID("01920000-0000-7000-8000-0000000000aa")
    b = uuid.UUID("01920000-0000-7000-8000-0000000000bb")
    await _message(_DM, at=_at(9), message_id=a)
    await _message(_DM, at=_at(9), message_id=b)
    row = await find_unread_summary(
        **_unread_args(_DM, after_ms=_ms(_at(9)), after_id=str(a)),
        bot_user_ids=[str(_AKAO_BOT_UID)],
    )
    assert row["unread"] == 1


async def test_named_you_is_null_when_nobody_computed_the_mention_column(bot_db):
    """NULL ≠ 没点她：加列前的存量行没人算过，不能当成"确认没点她"。"""
    await _seed_her_phone()
    await _message(_GROUP, at=_at(9), mentions=None, scope="group")
    row = await find_unread_summary(
        **_unread_args(_GROUP), bot_user_ids=[str(_AKAO_BOT_UID)]
    )
    assert row["named_you"] is None


async def test_named_you_is_true_when_the_mention_column_holds_her_bot(bot_db):
    await _seed_her_phone()
    await _message(_GROUP, at=_at(9), mentions=[_AKAO_BOT_UID], scope="group")
    row = await find_unread_summary(
        **_unread_args(_GROUP), bot_user_ids=[str(_AKAO_BOT_UID)]
    )
    assert row["named_you"] is True


async def test_named_you_is_false_when_someone_else_was_named(bot_db):
    await _seed_her_phone()
    await _message(_GROUP, at=_at(9), mentions=[_AYANA_BOT_UID], scope="group")
    row = await find_unread_summary(
        **_unread_args(_GROUP), bot_user_ids=[str(_AKAO_BOT_UID)]
    )
    assert row["named_you"] is False


async def test_unread_senders_are_ordered_by_who_spoke_last(bot_db):
    await _seed_her_phone()
    await _message(_GROUP, at=_at(9), who="甲", scope="group")
    await _message(_GROUP, at=_at(11), who="乙", scope="group")
    await _message(_GROUP, at=_at(10), who="丙", scope="group")
    rows = await find_unread_senders(**_unread_args(_GROUP), limit=2)
    assert [r["who"] for r in rows] == ["乙", "丙"]


async def test_an_unnamed_sender_reads_as_someone(bot_db):
    await _seed_her_phone()
    await _message(_DM, at=_at(9), who=None)
    rows = await find_unread_senders(**_unread_args(_DM), limit=4)
    assert [r["who"] for r in rows] == ["某人"]


# ---------------------------------------------------------------------------
# 谁在叫她
# ---------------------------------------------------------------------------


async def test_any_unread_line_in_a_direct_conversation_is_a_summons(bot_db):
    await _seed_her_phone()
    mid = await _message(_DM, at=_at(9))
    row = await find_newest_unread_summons(
        **_unread_args(_DM), is_direct=True, bot_user_ids=[str(_AKAO_BOT_UID)]
    )
    assert str(row["message_id"]) == str(mid)
    assert row["at_ms"] == _ms(_at(9))


async def test_group_chatter_without_her_name_is_not_a_summons(bot_db):
    await _seed_her_phone()
    await _message(_GROUP, at=_at(9), mentions=[], scope="group")
    row = await find_newest_unread_summons(
        **_unread_args(_GROUP), is_direct=False, bot_user_ids=[str(_AKAO_BOT_UID)]
    )
    assert row is None


async def test_being_named_in_a_group_is_a_summons(bot_db):
    await _seed_her_phone()
    mid = await _message(
        _GROUP, at=_at(9), mentions=[_AKAO_BOT_UID], scope="group"
    )
    row = await find_newest_unread_summons(
        **_unread_args(_GROUP), is_direct=False, bot_user_ids=[str(_AKAO_BOT_UID)]
    )
    assert str(row["message_id"]) == str(mid)


async def test_the_newest_summons_wins(bot_db):
    await _seed_her_phone()
    await _message(_DM, at=_at(9))
    newest = await _message(_DM, at=_at(11))
    row = await find_newest_unread_summons(
        **_unread_args(_DM), is_direct=True, bot_user_ids=[str(_AKAO_BOT_UID)]
    )
    assert str(row["message_id"]) == str(newest)


# ---------------------------------------------------------------------------
# 打开一条会话：窗口 W、未读总数 |U|、max(U) 一条语句给全
# ---------------------------------------------------------------------------


async def test_the_window_is_two_way_and_ignores_the_cursor(bot_db):
    """窗口回答"这条会话最近说了些什么"，一行都不会因为读过了被挡在外面。"""
    await _seed_her_phone()
    await _message(_DM, at=_at(9), body="早")
    await _message(
        _DM, at=_at(9, 30), role="assistant", bot_name="chiwei", body="早啊"
    )
    await _message(_DM, at=_at(10), body="在吗")
    rows = await find_conversation_window(
        **_unread_args(_DM, after_ms=_ms(_at(9, 30)), after_id="z"), limit=10
    )
    assert len(rows) == 3
    assert [r["is_unread"] for r in rows] == [True, False, False]


async def test_the_window_marks_which_rows_are_still_unread(bot_db):
    await _seed_her_phone()
    await _message(_DM, at=_at(9))
    await _message(_DM, at=_at(10))
    rows = await find_conversation_window(
        **_unread_args(_DM, after_ms=_ms(_at(9)), after_id="z"), limit=10
    )
    assert rows[0]["unread_total"] == 1
    assert rows[0]["newest_unread_ms"] == _ms(_at(10))


async def test_her_own_recalled_line_stays_visible_when_she_opens_it(bot_db):
    """她自己撤掉的那条留一条痕迹并带原话；别人撤掉的仍然不显示。"""
    await _seed_her_phone()
    await _message(
        _DM,
        at=_at(9),
        role="assistant",
        bot_name="chiwei",
        body="说错了",
        recalled_at=_at(9, 1),
    )
    await _message(_DM, at=_at(10), recalled_at=_at(10, 1))
    rows = await find_conversation_window(**_unread_args(_DM), limit=10)
    assert len(rows) == 1
    assert rows[0]["said_by_you"] is True
    assert rows[0]["recalled_at"] is not None


async def test_the_window_keeps_the_most_recent_n(bot_db):
    await _seed_her_phone()
    for i in range(5):
        await _message(_DM, at=_at(9, i), body=f"第{i}条")
    rows = await find_conversation_window(**_unread_args(_DM), limit=3)
    assert [r["at_ms"] for r in rows] == [
        _ms(_at(9, 4)),
        _ms(_at(9, 3)),
        _ms(_at(9, 2)),
    ]


async def test_the_window_carries_the_handle_she_can_take_a_line_back_with(bot_db):
    await _seed_her_phone()
    oid = uuid.uuid4()
    await _message(
        _DM, at=_at(9), role="assistant", bot_name="chiwei", outbound_id=oid
    )
    rows = await find_conversation_window(**_unread_args(_DM), limit=10)
    assert str(rows[0]["outbound_id"]) == str(oid)


async def test_an_empty_conversation_yields_no_window_rows(bot_db):
    await _seed_her_phone()
    assert await find_conversation_window(**_unread_args(_DM), limit=10) == []


# ---------------------------------------------------------------------------
# 她已经知道的那一段（嘴渲染措辞时看的全部）
# ---------------------------------------------------------------------------


async def test_what_she_knows_stops_at_the_cursor(bot_db):
    await _seed_her_phone()
    await _message(_DM, at=_at(9), body="读过的")
    await _message(_DM, at=_at(11), body="还没读的")
    rows = await find_messages_known_through(
        channel_id=str(_DM),
        cursor_ms=_ms(_at(10)),
        cursor_id="z",
        own_bots=["chiwei", "chiwei-dev"],
        limit=20,
    )
    assert [r["content_text"] for r in rows] == ["读过的"]


async def test_her_own_lines_walk_past_the_cursor(bot_db):
    """她当然知道自己说过什么 —— 但只有她自己的话走得进这道门。"""
    await _seed_her_phone()
    await _message(
        _DM, at=_at(11), role="assistant", bot_name="chiwei", body="我刚说的"
    )
    await _message(
        _GROUP,
        at=_at(11),
        role="assistant",
        bot_name="ayana",
        body="姐姐说的",
        scope="group",
    )
    mine = await find_messages_known_through(
        channel_id=str(_DM),
        cursor_ms=0,
        cursor_id="",
        own_bots=["chiwei", "chiwei-dev"],
        limit=20,
    )
    assert [r["content_text"] for r in mine] == ["我刚说的"]
    assert mine[0]["said_by_you"] is True

    sisters = await find_messages_known_through(
        channel_id=str(_GROUP),
        cursor_ms=0,
        cursor_id="",
        own_bots=["chiwei", "chiwei-dev"],
        limit=20,
    )
    assert sisters == []


async def test_a_recalled_line_is_gone_from_what_she_knows(bot_db):
    await _seed_her_phone()
    await _message(
        _DM,
        at=_at(9),
        role="assistant",
        bot_name="chiwei",
        body="撤掉的",
        recalled_at=_at(9, 1),
    )
    rows = await find_messages_known_through(
        channel_id=str(_DM),
        cursor_ms=_ms(_at(10)),
        cursor_id="z",
        own_bots=["chiwei", "chiwei-dev"],
        limit=20,
    )
    assert rows == []


# ---------------------------------------------------------------------------
# 按名字找回一条会话
# ---------------------------------------------------------------------------


async def test_a_group_is_found_by_its_title(bot_db):
    await _seed_her_phone()
    rows = await search_persona_conversations_by_name(
        persona_id="akao", name_like="%实验%", own_bots=["chiwei", "chiwei-dev"]
    )
    assert [str(r["channel_id"]) for r in rows] == [str(_GROUP)]


async def test_a_direct_conversation_is_found_by_who_spoke_in_it(bot_db):
    """私聊多半没有标题，只查标题等于查不到人。"""
    await _seed_her_phone()
    await _message(_DM, at=_at(9), who="bezhai")
    rows = await search_persona_conversations_by_name(
        persona_id="akao", name_like="%bezhai%", own_bots=["chiwei", "chiwei-dev"]
    )
    assert [str(r["channel_id"]) for r in rows] == [str(_DM)]
    assert list(rows[0]["matched"]) == ["bezhai"]
    assert rows[0]["latest"] == _ms(_at(9))


async def test_her_own_name_does_not_match_a_conversation(bot_db):
    await _seed_her_phone()
    await _message(
        _DM, at=_at(9), role="assistant", bot_name="chiwei", who="赤尾"
    )
    rows = await search_persona_conversations_by_name(
        persona_id="akao", name_like="%赤尾%", own_bots=["chiwei", "chiwei-dev"]
    )
    assert rows == []


async def test_the_search_never_leaves_her_own_phone(bot_db):
    await _seed_her_phone()
    await _message(_NOT_HERS, at=_at(9), who="bezhai", scope="group")
    rows = await search_persona_conversations_by_name(
        persona_id="akao", name_like="%bezhai%", own_bots=["chiwei", "chiwei-dev"]
    )
    assert rows == []


# ---------------------------------------------------------------------------
# 有人发到她手机上的文件
# ---------------------------------------------------------------------------

_FILE_CONTENT = [
    {"kind": "text", "text": "看看这本"},
    {
        "kind": "file",
        "key": "file_v3_abc",
        "meta": {"file_name": "沉默的大多数.epub"},
    },
]


async def test_a_file_sent_to_her_is_found_with_its_original_name(bot_db):
    await _seed_her_phone()
    await _message(_DM, at=_at(9), who="bezhai", content=_FILE_CONTENT)
    rows = await find_file_items_in_persona_conversations("akao")
    assert len(rows) == 1
    assert rows[0]["file_key"] == "file_v3_abc"
    assert rows[0]["file_name"] == "沉默的大多数.epub"
    assert rows[0]["who"] == "bezhai"
    assert rows[0]["scope"] == "direct"
    assert rows[0]["still_gettable"] is True


async def test_a_recalled_file_is_reported_not_filtered(bot_db):
    """撤回改变的是"现在还能不能拿到"，不是"有没有发生过" —— 判定留给调用方。"""
    await _seed_her_phone()
    await _message(
        _DM, at=_at(9), content=_FILE_CONTENT, recalled_at=_at(9, 1)
    )
    rows = await find_file_items_in_persona_conversations("akao")
    assert len(rows) == 1
    assert rows[0]["still_gettable"] is False


async def test_a_message_without_a_file_item_is_not_a_file(bot_db):
    await _seed_her_phone()
    await _message(_DM, at=_at(9), content=[{"kind": "image", "key": "img_1"}])
    assert await find_file_items_in_persona_conversations("akao") == []


async def test_files_from_conversations_that_are_not_hers_stay_out(bot_db):
    await _seed_her_phone()
    await _message(_NOT_HERS, at=_at(9), content=_FILE_CONTENT, scope="group")
    assert await find_file_items_in_persona_conversations("akao") == []


async def test_two_bots_in_one_conversation_do_not_double_a_file(bot_db):
    """一个 persona 名下挂着好几个 bot，不去重同一个文件就会列好几遍。"""
    await _seed_her_phone()
    await _present(_DM, "chiwei-dev")
    await _message(_DM, at=_at(9), content=_FILE_CONTENT)
    assert len(await find_file_items_in_persona_conversations("akao")) == 1


async def test_files_come_back_newest_first(bot_db):
    await _seed_her_phone()
    await _message(_DM, at=_at(9), content=_FILE_CONTENT)
    await _message(
        _DM,
        at=_at(11),
        content=[
            {"kind": "file", "key": "file_v3_zzz", "meta": {"file_name": "新的.txt"}}
        ],
    )
    rows = await find_file_items_in_persona_conversations("akao")
    assert [r["file_key"] for r in rows] == ["file_v3_zzz", "file_v3_abc"]


# ---------------------------------------------------------------------------
# 她那次开口在公共层落成了什么（对账那条钟读的）
# ---------------------------------------------------------------------------


async def test_the_row_she_spoke_into_is_found_by_outbound_id(bot_db):
    await _seed_her_phone()
    oid = uuid.uuid4()
    mid = await _message(
        _DM, at=_at(9), role="assistant", bot_name="chiwei", outbound_id=oid
    )
    rows = await find_messages_by_outbound_ids([oid])
    assert len(rows) == 1
    assert rows[0]["agent_outbound_id"] == oid
    assert str(rows[0]["common_message_id"]) == str(mid)
    assert rows[0]["event_time"] == _ms(_at(9))


async def test_several_rows_for_one_outbound_come_back_oldest_first(bot_db):
    """一次开口只该落一行；真出现两行时调用方要取最早那条。"""
    await _seed_her_phone()
    oid = uuid.uuid4()
    await _message(
        _DM, at=_at(11), role="assistant", bot_name="chiwei", outbound_id=oid
    )
    await _message(
        _DM, at=_at(9), role="assistant", bot_name="chiwei", outbound_id=oid
    )
    rows = await find_messages_by_outbound_ids([oid])
    assert [r["event_time"] for r in rows] == [_ms(_at(9)), _ms(_at(11))]


async def test_an_outbound_that_never_landed_is_simply_absent(bot_db):
    await _seed_her_phone()
    assert await find_messages_by_outbound_ids([uuid.uuid4()]) == []


async def test_recall_state_counts_parts_and_how_many_are_gone(bot_db):
    """一次开口切成几段发出去时每段各一行；撤掉一段不等于整条撤完。"""
    await _seed_her_phone()
    oid = uuid.uuid4()
    await _message(
        _DM,
        at=_at(9),
        role="assistant",
        bot_name="chiwei",
        outbound_id=oid,
        recalled_at=_at(9, 30),
    )
    await _message(
        _DM, at=_at(9, 1), role="assistant", bot_name="chiwei", outbound_id=oid
    )
    rows = await find_recall_state_by_outbound_ids([oid])
    assert len(rows) == 1
    assert rows[0]["parts"] == 2
    assert rows[0]["parts_recalled"] == 1


async def test_recall_state_reports_the_last_part_that_went_away(bot_db):
    await _seed_her_phone()
    oid = uuid.uuid4()
    await _message(
        _DM,
        at=_at(9),
        role="assistant",
        bot_name="chiwei",
        outbound_id=oid,
        recalled_at=_at(9, 30),
    )
    await _message(
        _DM,
        at=_at(9, 1),
        role="assistant",
        bot_name="chiwei",
        outbound_id=oid,
        recalled_at=_at(9, 40),
    )
    rows = await find_recall_state_by_outbound_ids([oid])
    assert rows[0]["parts"] == rows[0]["parts_recalled"] == 2
    assert rows[0]["last_recalled_at"] == _at(9, 40)
