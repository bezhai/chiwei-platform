"""异步阅读 @node + durable 触发信号契约 — 读小说 Task 2.

她在 life 轮里调读书工具认准一个文件 → emit 一个 durable ``ReadingTriggered``（立即 emit、
非定时器，仿 act 的 durable 范式）→ 这个 @node 消费它跑阅读任务。触发携带的是**附件实例
身份**（收到该文件那次）+ 对象存储引用 + 文件名，不再是注册的 book_id。本文件钉住 @node
外壳的正确性（spec key decision 2 的 durable 幂等 + CAS 三条）：

  * **turn 幂等查重跳过**：触发携带 ``request_id``；@node 在跑昂贵的阅读 agent 之前先查
    它是否已提交过（已落在印象行上），提交过就跳过、不重复跑 agent。
  * **印象 + 页号提交走版本 CAS**：跑完阅读 agent 后读印象当时的 ver，
    ``save_impression(expected_ver=)`` 条件写入——并发 / 过期任务不覆盖更新的印象。
  * **fail-soft**：阅读 agent 返回 None → 印象 / 页号都不动、不提交。
  * **读到书尾置「读完」**：阅读 agent 报 finished → 状态置 finished、页号不越界。

阅读 agent（``run_reading_round``）整个 mock 掉，测的是外壳编排（幂等查重、CAS、fail-soft、
书尾置读完、进度提交、按附件实例身份 key），不真跑 agent。
"""

from __future__ import annotations

import pytest

import app.nodes.reading as rn
from app.domain.book_impression import (
    STATUS_FINISHED,
    STATUS_READING,
    BookImpression,
)

_LANE = "coe-t2"
_PERSONA = "akao"
_ATTACHMENT = "msg-1:file-k"
_TOS = "files/file-k"
_FILE_NAME = "斜阳.txt"
_REQ = "req-111"


def _trigger(**over):
    base = dict(
        lane=_LANE,
        persona_id=_PERSONA,
        attachment_id=_ATTACHMENT,
        book_title=_FILE_NAME,
        tos_file=_TOS,
        file_name=_FILE_NAME,
        request_id=_REQ,
        occurred_at="2026-06-23T12:00:00+08:00",
    )
    base.update(over)
    return rn.ReadingTriggered(**base)


@pytest.fixture
def fake_store(monkeypatch):
    """印象存储打桩：内存里存「当前印象」+ 记录所有 save 调用（含 expected_ver + 自然键）。"""
    store: dict = {"current": None, "saves": [], "save_ok": True}

    async def fake_find(*, lane, persona_id, attachment_id):
        store["find_key"] = attachment_id
        return store["current"]

    async def fake_save(*, lane, persona_id, attachment_id, book_title, impression,
                        pages_read, status, observed_at, expected_ver, request_id=""):
        store["saves"].append(
            {
                "attachment_id": attachment_id,
                "book_title": book_title,
                "impression": impression,
                "pages_read": pages_read,
                "status": status,
                "expected_ver": expected_ver,
                "request_id": request_id,
            }
        )
        if not store["save_ok"]:
            return False
        new_ver = (store["current"].ver if store["current"] else 0) + 1
        store["current"] = BookImpression(
            lane=lane, persona_id=persona_id, attachment_id=attachment_id,
            book_title=book_title, ver=new_ver,
            impression=impression, pages_read=pages_read, status=status,
            observed_at=observed_at, last_request_id=request_id,
        )
        return True

    monkeypatch.setattr(rn, "find_book_impression", fake_find)
    monkeypatch.setattr(rn, "save_impression", fake_save)
    return store


@pytest.fixture
def fake_agent(monkeypatch):
    """run_reading_round 打桩：返回预置 ReadingResult / None，记录被调用了几次 + 入参。"""
    from app.agent.reading import ReadingResult

    calls: dict = {"n": 0, "args": [], "result": ReadingResult(
        impression="读完一程的新印象。", pages_read=4, finished=False
    )}

    async def fake_run(*, lane, persona_id, attachment_id, book_title, tos_file,
                       file_name, prior_impression, start_page, round_id):
        calls["n"] += 1
        calls["args"].append(
            {"attachment_id": attachment_id, "tos_file": tos_file,
             "file_name": file_name, "book_title": book_title,
             "prior_impression": prior_impression, "start_page": start_page,
             "round_id": round_id}
        )
        return calls["result"]

    monkeypatch.setattr(rn, "run_reading_round", fake_run)
    return calls


# ---------------------------------------------------------------------------
# 触发信号形态：durable（仿 act），携带附件实例身份 + 对象存储引用 + 文件名
# ---------------------------------------------------------------------------


def test_trigger_is_durable_carries_request_id():
    """ReadingTriggered 是 durable（非 transient）—— 立即 emit、跨进程可达不丢（仿 act）。"""
    meta = getattr(rn.ReadingTriggered, "Meta", None)
    assert not (meta and getattr(meta, "transient", False)), \
        "触发信号是 durable（仿 ActPerformed），不是 transient 定时器"
    from app.runtime.data import key_fields
    keys = list(key_fields(rn.ReadingTriggered))
    assert "lane" in keys, "lane 进 Key（泳道隔离）"
    assert "request_id" in keys, "request_id 进 Key（重投幂等去重）"


def test_trigger_carries_attachment_identity_not_book_id():
    """触发携带附件实例身份 + 对象存储引用 + 文件名（不再是注册的 book_id）。"""
    trig = _trigger()
    assert trig.attachment_id == _ATTACHMENT
    assert trig.tos_file == _TOS, "对象存储引用，读时按它取字节"
    assert trig.file_name == _FILE_NAME, "原始文件名，解码分流靠它"
    assert not hasattr(trig, "book_id"), "不再携带注册的 book_id"


@pytest.mark.integration
async def test_trigger_migrates_and_persists_end_to_end(test_db):
    """新 durable Data 端到端：framework migrate 建表 + insert 落得进、读得回。"""
    from app.runtime.persist import insert_idempotent, select_latest
    from tests.runtime.conftest import migrate

    await migrate(rn.ReadingTriggered, test_db)
    trig = _trigger()
    n1 = await insert_idempotent(trig)
    assert n1 == 1, "首次落库一行"
    n2 = await insert_idempotent(_trigger())
    assert n2 == 0, "重投按 (lane, request_id) 去重不重复落"
    got = await select_latest(
        rn.ReadingTriggered, {"lane": _LANE, "request_id": _REQ}
    )
    assert got is not None
    assert got.attachment_id == _ATTACHMENT and got.book_title == _FILE_NAME
    assert got.tos_file == _TOS


# ---------------------------------------------------------------------------
# 正常一程：跑 agent → CAS 提交印象 + 页号（按附件实例身份 key）
# ---------------------------------------------------------------------------


async def test_first_round_runs_agent_and_commits(fake_store, fake_agent):
    """首次读这个文件（无旧印象）→ 跑 agent、CAS 提交（expected_ver=0）、状态在读。"""
    await rn.reading_node(_trigger())
    assert fake_agent["n"] == 1
    # 喂给 agent 的是附件实例身份 + tos_file + file_name + [无旧印象 + 从第 0 页接着读]
    assert fake_agent["args"][0]["attachment_id"] == _ATTACHMENT
    assert fake_agent["args"][0]["tos_file"] == _TOS
    assert fake_agent["args"][0]["file_name"] == _FILE_NAME
    assert fake_agent["args"][0]["prior_impression"] is None
    assert fake_agent["args"][0]["start_page"] == 0
    # CAS 提交：按附件实例身份 key、首次 expected_ver=0、书名自带
    assert len(fake_store["saves"]) == 1
    save = fake_store["saves"][0]
    assert save["attachment_id"] == _ATTACHMENT
    assert save["book_title"] == _FILE_NAME, "书名由印象自带（不查书注册表）"
    assert save["expected_ver"] == 0
    assert save["status"] == STATUS_READING
    assert save["pages_read"] == 4


async def test_impression_keyed_by_attachment_instance(fake_store, fake_agent):
    """印象按附件实例身份 key（find + save 都用 attachment_id）。"""
    await rn.reading_node(_trigger())
    assert fake_store["find_key"] == _ATTACHMENT
    assert fake_store["saves"][0]["attachment_id"] == _ATTACHMENT


async def test_continue_round_feeds_prior_and_advances_from_pages_read(
    fake_store, fake_agent
):
    """续读：喂 agent 当前印象 + 从 pages_read 接着读；CAS 用当前 ver。"""
    fake_store["current"] = BookImpression(
        lane=_LANE, persona_id=_PERSONA, attachment_id=_ATTACHMENT,
        book_title=_FILE_NAME, ver=2,
        impression="旧印象。", pages_read=4, status=STATUS_READING,
        observed_at="t", last_request_id="old-req",
    )
    await rn.reading_node(_trigger())
    assert fake_agent["args"][0]["prior_impression"] == "旧印象。"
    assert fake_agent["args"][0]["start_page"] == 4, "从当前 pages_read 接着读"
    assert fake_store["saves"][0]["expected_ver"] == 2, "CAS 用读到的当前 ver"


# ---------------------------------------------------------------------------
# turn 幂等：request_id 已提交过 → 跳过
# ---------------------------------------------------------------------------


async def test_already_committed_request_skips_agent(fake_store, fake_agent):
    """同一 request_id 已提交过 → 跳过、不再跑 agent（turn 幂等）。"""
    fake_store["current"] = BookImpression(
        lane=_LANE, persona_id=_PERSONA, attachment_id=_ATTACHMENT,
        book_title=_FILE_NAME, ver=3,
        impression="这一程已经读过了。", pages_read=8, status=STATUS_READING,
        observed_at="t", last_request_id=_REQ,
    )
    await rn.reading_node(_trigger(request_id=_REQ))
    assert fake_agent["n"] == 0
    assert fake_store["saves"] == []


async def test_different_request_id_does_run(fake_store, fake_agent):
    """不同 request_id（另一次开读）→ 照常跑。"""
    fake_store["current"] = BookImpression(
        lane=_LANE, persona_id=_PERSONA, attachment_id=_ATTACHMENT,
        book_title=_FILE_NAME, ver=3,
        impression="上一程。", pages_read=8, status=STATUS_READING,
        observed_at="t", last_request_id="old-req",
    )
    await rn.reading_node(_trigger(request_id="req-NEW"))
    assert fake_agent["n"] == 1


# ---------------------------------------------------------------------------
# fail-soft：agent 返回 None → 不提交
# ---------------------------------------------------------------------------


async def test_agent_none_does_not_commit(fake_store, fake_agent):
    """阅读 agent fail-soft 返回 None（取不到字节 / 超时 / 抛错 / 空产出）→ 不提交。"""
    fake_agent["result"] = None
    await rn.reading_node(_trigger())
    assert fake_store["saves"] == [], "agent 失败本程不算，绝不写半截脏印象"


# ---------------------------------------------------------------------------
# 读到书尾 → 状态置「读完」
# ---------------------------------------------------------------------------


async def test_reaching_end_commits_finished(fake_store, fake_agent):
    """阅读 agent 报 finished → 状态置「读完」、页号来自 agent（不越界）。"""
    from app.agent.reading import ReadingResult
    fake_agent["result"] = ReadingResult(
        impression="读到结尾了，心里空落落的。", pages_read=20, finished=True
    )
    await rn.reading_node(_trigger())
    save = fake_store["saves"][0]
    assert save["status"] == STATUS_FINISHED
    assert save["pages_read"] == 20


# ---------------------------------------------------------------------------
# CAS 落败（并发抢先）：不报错、不重复跑
# ---------------------------------------------------------------------------


async def test_cas_lost_race_is_fail_soft(fake_store, fake_agent):
    """CAS 写入落败（save 返回 False）→ 不炸、本程作废（她可重读）。"""
    fake_store["save_ok"] = False
    await rn.reading_node(_trigger())
    assert len(fake_store["saves"]) == 1
    assert fake_store["current"] is None
