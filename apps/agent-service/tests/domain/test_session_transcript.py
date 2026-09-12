"""Transcript 存储契约 —— PG durable Data，真 pg 上验。

存的是一整条可回放的 ``Message`` 序列（含 tool call / result / 各 provider 私有 blob
如 gemini ``thought_signature``），按 ``session_id`` 取最新一版。

契约四条：
  - ``load_session`` 读最新一版 + 它的版本号；没有记录（首次 / 清库后）→ ``([], 0)``，
    绝不抛错；``transcript_json`` 坏掉 → log.warning + 空上下文，但报真实版本号。
  - ``replace_session`` 按调用方给的 ``expected_ver`` 做 CAS：库里已经不是那一版就
    一行都不写、返回 ``False``。落地即新一版，旧版留作可查历史。
  - serialise → PG → deserialise → model 必须 lossless（signature / 多模态不丢）。
  - **存储层不做任何裁剪**：交多少存多少，原样读回来。裁剪只有一处，在
    :mod:`app.living.continuity`。

集成测试（真实 Postgres，testcontainers）：整个正确性故事是写出新版本、
``select_latest`` 取最新全文、CAS 拦住过期写、跨 session 不串——mock pg 等于什么都没测。
"""

from __future__ import annotations

import json
import logging

import pytest

from app.agent.neutral import ContentBlock, Message, Role, ToolCall
from app.agent.session import load_session, replace_session
from app.domain.session_transcript import SessionTranscript
from app.runtime.persist import insert_append, select_all_versions, select_latest
from tests.runtime.conftest import migrate

pytestmark = pytest.mark.integration


@pytest.fixture
async def session_db(test_db):
    """Build the SessionTranscript table on the test db."""
    await migrate(SessionTranscript, test_db)
    yield test_db


# ---------------------------------------------------------------------------
# Cold start
# ---------------------------------------------------------------------------


async def test_load_missing_session_returns_empty_at_version_zero(session_db):
    # first time / cleared db → cold start, no error. ver 0 is the base a first
    # write CASes against.
    assert await load_session("coe-x:akao:2026-06-04") == ([], 0)


# ---------------------------------------------------------------------------
# Round-trip: write then load
# ---------------------------------------------------------------------------


async def test_write_then_load_roundtrips_messages(session_db):
    sid = "coe-x:akao:2026-06-04"
    msgs = [
        Message(role=Role.USER, content="你醒了，现在是晚餐时间"),
        Message(role=Role.ASSISTANT, content="我看看餐桌"),
    ]
    assert await replace_session(sid, msgs, expected_ver=0) is True

    loaded, ver = await load_session(sid)
    assert ver == 1
    assert [m.role for m in loaded] == [Role.USER, Role.ASSISTANT]
    assert loaded[0].text() == "你醒了，现在是晚餐时间"
    assert loaded[1].text() == "我看看餐桌"


async def test_each_write_is_a_new_version_and_latest_is_the_whole_transcript(
    session_db,
):
    sid = "coe-x:akao:2026-06-04"
    first = [Message(role=Role.USER, content="a")]
    await replace_session(sid, first, expected_ver=0)
    second = [*first, Message(role=Role.ASSISTANT, content="b")]
    await replace_session(sid, second, expected_ver=1)
    third = [*second, Message(role=Role.USER, content="c")]
    await replace_session(sid, third, expected_ver=2)

    versions = await select_all_versions(SessionTranscript, {"session_id": sid})
    assert [v.ver for v in versions] == [1, 2, 3]

    latest = await select_latest(SessionTranscript, {"session_id": sid})
    payload = json.loads(latest.transcript_json)
    assert [d["content"] for d in payload] == ["a", "b", "c"]


async def test_an_empty_transcript_is_refused(session_db):
    # storing [] would erase her context; no caller means it.
    with pytest.raises(ValueError):
        await replace_session("coe-x:akao:2026-06-04", [], expected_ver=0)


# ---------------------------------------------------------------------------
# CAS: a write computed from a stale read must not land
# ---------------------------------------------------------------------------


async def test_a_stale_expected_version_writes_nothing(session_db):
    sid = "coe-x:akao:2026-06-04"
    await replace_session(
        sid, [Message(role=Role.USER, content="第一版")], expected_ver=0
    )

    landed = await replace_session(
        sid, [Message(role=Role.USER, content="拿旧版本覆盖")], expected_ver=0
    )
    assert landed is False

    loaded, ver = await load_session(sid)
    assert ver == 1
    assert [m.text() for m in loaded] == ["第一版"]


# ---------------------------------------------------------------------------
# Lossless replay: tool calls + results + provider signature survive PG
# ---------------------------------------------------------------------------


async def test_tool_call_and_result_with_signature_survive_roundtrip(session_db):
    sid = "coe-x:akao:2026-06-04"
    msgs = [
        Message(role=Role.USER, content="该广播了吗"),
        Message(
            role=Role.ASSISTANT,
            content="",
            reasoning_content="想了想",
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="emit_event",
                    arguments={"summary": "晚餐进行中"},
                    signature=b"\x00\xff gemini-thought",
                )
            ],
        ),
        Message(role=Role.TOOL, content="emitted", tool_call_id="c1"),
    ]
    await replace_session(sid, msgs, expected_ver=0)

    loaded, _ver = await load_session(sid)
    assistant = next(m for m in loaded if m.role == Role.ASSISTANT)
    assert assistant.reasoning_content == "想了想"
    assert assistant.tool_calls[0].arguments == {"summary": "晚餐进行中"}
    # the provider-private blob must NOT be lost — replay would drift otherwise.
    assert assistant.tool_calls[0].signature == b"\x00\xff gemini-thought"
    tool_msg = next(m for m in loaded if m.role == Role.TOOL)
    assert tool_msg.tool_call_id == "c1"


async def test_multimodal_content_survives_roundtrip(session_db):
    sid = "coe-x:akao:2026-06-04"
    msgs = [
        Message(
            role=Role.TOOL,
            content=[
                ContentBlock.from_text("@3.png:"),
                ContentBlock.from_image_url({"url": "https://x/3.png"}),
            ],
            tool_call_id="c1",
        ),
    ]
    await replace_session(sid, msgs, expected_ver=0)
    loaded, _ver = await load_session(sid)
    assert isinstance(loaded[0].content, list)
    assert loaded[0].content[1].image_url == {"url": "https://x/3.png"}


# ---------------------------------------------------------------------------
# Corrupt transcript_json → log + cold start (never crash the run), but the
# real version still comes back: the caller has to CAS against what is there.
# ---------------------------------------------------------------------------


async def test_corrupt_transcript_json_cold_starts_with_warning(session_db, caplog):
    sid = "coe-x:akao:2026-06-04"
    await insert_append(
        SessionTranscript(session_id=sid, transcript_json="{not json at all")
    )
    with caplog.at_level(logging.WARNING):
        loaded, ver = await load_session(sid)
    assert loaded == []
    assert ver == 1
    assert any("transcript" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# No trimming: the store keeps what it is handed
# ---------------------------------------------------------------------------


async def test_the_store_never_trims_what_it_is_handed(session_db):
    """存储层不裁剪，这是"裁剪只有一处"的存储侧断言。

    旧实现在这里有两条上限（200 条 / 256 KiB），行为是丢最老的 + 记一行警告、返回值
    看不出来。裁剪归 :mod:`app.living.continuity` 一处管之后，这一层交多少存多少。
    """
    sid = "coe-x:akao:2026-06-04"
    filler = "x" * 4096
    msgs = [Message(role=Role.USER, content=f"m{i}-{filler}") for i in range(600)]
    await replace_session(sid, msgs, expected_ver=0)

    loaded, _ver = await load_session(sid)
    assert [m.text() for m in loaded] == [m.text() for m in msgs]


# ---------------------------------------------------------------------------
# Lane / session isolation: session_id already carries lane (lane:actor:date),
# so different lanes are different session_ids → different keys → never串.
# This is the framework 三步检查第 3 步的端到端隔离断言.
# ---------------------------------------------------------------------------


async def test_different_session_ids_do_not_cross_contaminate(session_db):
    prod_sid = "prod:akao:2026-06-04"
    coe_sid = "coe-x:akao:2026-06-04"
    await replace_session(
        prod_sid, [Message(role=Role.USER, content="prod-意识流")], expected_ver=0
    )
    await replace_session(
        coe_sid, [Message(role=Role.USER, content="coe-意识流")], expected_ver=0
    )

    prod_loaded, _ = await load_session(prod_sid)
    coe_loaded, _ = await load_session(coe_sid)
    assert [m.text() for m in prod_loaded] == ["prod-意识流"]
    assert [m.text() for m in coe_loaded] == ["coe-意识流"]


async def test_different_actors_same_lane_do_not_cross_contaminate(session_db):
    akao_sid = "coe-x:akao:2026-06-04"
    world_sid = "coe-x:world:2026-06-04"
    await replace_session(
        akao_sid, [Message(role=Role.USER, content="akao 的")], expected_ver=0
    )
    await replace_session(
        world_sid, [Message(role=Role.USER, content="world 的")], expected_ver=0
    )

    assert (await load_session(akao_sid))[0][0].text() == "akao 的"
    assert (await load_session(world_sid))[0][0].text() == "world 的"
