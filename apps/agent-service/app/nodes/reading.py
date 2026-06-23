"""异步阅读 @node + durable 触发信号 — 读小说 Task 2.

她在 life 轮里调读书工具认准一本书 → emit 一个 durable :class:`ReadingTriggered`
（**立即 emit、非定时器**，仿 :class:`app.domain.world_events.ActPerformed` 的 durable
范式：落 PG 跨进程可达、不丢）→ 这个 @node 消费它跑一程阅读任务。

@node 外壳负责 spec key decision 2 的 durable 幂等三条（最承重）：

  1. **turn 幂等查重**：触发携带从 life 触发轮派生的 ``request_id``。@node 在跑**昂贵的
     阅读 agent 之前**先读当前印象、若它的 ``last_request_id`` 已等于本次 request_id，
     说明这一程已提交过 → 跳过、不重复跑 agent（仿 life_wake 的 round marker 查重，但
     marker 落在 durable 印象行上）。挡住 durable 重投 / 整轮重试重复烧一程钱。

  2. **印象 + 页号提交走版本 CAS**：跑完阅读 agent 后，用读印象时记下的 ``ver`` 做
     ``save_impression(expected_ver=)`` 条件写入——并发 / 过期任务（拿着过时印象的重放）
     不覆盖更新的印象、不双推进页号（仿 ``replace_session(expected_ver=)``）。

  3. **真正的 durable mutation 在 agent 返回之后做**：阅读 agent 的 ``read`` 工具是只读的
     （无 durable mutation），所以 agent 本身可安全 retry；写印象 + 推进页号只在这里、在
     ``run_reading_round`` 返回**之后**做一次 CAS 提交。

**fail-soft**（spec）：阅读 agent 返回 None（超时 / 抛错 / 空产出）→ 印象 / 页号都不动、
不提交（她可重读）。CAS 写入落败（并发抢先）→ 不炸、本程作废（同样她可重读）。绝不写
半截脏印象。

**读到书尾置「读完」**：阅读 agent 报 ``finished`` → 状态置 :data:`STATUS_FINISHED`、
页号取 agent 派生的（夹到 total、不越界）；否则状态保持 :data:`STATUS_READING`。

wiring 见 ``app/wiring/life_dataflow.py``（``wire(ReadingTriggered).to(reading_node)``）。
"""

from __future__ import annotations

import logging
from typing import Annotated

from app.agent.reading import run_reading_round  # module-level so tests can monkeypatch
from app.domain.book_impression import (
    STATUS_FINISHED,
    STATUS_READING,
    find_book_impression,  # module-level so tests can monkeypatch
    save_impression,  # module-level so tests can monkeypatch
)
from app.infra import cst_time
from app.runtime import node
from app.runtime.data import Data, Key

logger = logging.getLogger(__name__)


class ReadingTriggered(Data):
    """durable 触发信号：某 persona 在 life 轮里决定读某本书一程（仿 ActPerformed）。

    life 读书工具认准一本书后直接 ``emit(ReadingTriggered(...))``——**立即 emit、非
    定时器**（读读停停由她每次自己决定，不是起头后自走读完）。durable（非 transient）
    让它落 PG 跨进程可达（life 进程 emit、阅读 @node 进程消费）且不丢。

    自然键 ``(lane, request_id)``：``request_id`` 从 life 触发轮派生（仿 act_id 从 round
    event_ids 派生），durable 重投 / 整轮重试用同一 ``(lane, request_id)`` 再 emit 一次靠
    框架去重不重复消费；@node 内再用 request_id 做 turn 幂等查重（已提交过就跳过）。
    lane 进 Key 是泳道隔离硬约束（同其它 durable Data）。
    """

    lane: Annotated[str, Key]
    request_id: Annotated[str, Key]
    persona_id: str       # 谁要读
    book_id: str          # 读哪本（工具已模糊解析到确定的 book_id）
    book_title: str       # 书名（喂阅读 agent 的外壳用）
    occurred_at: str      # 她决定读这一程的时刻 (ISO8601)


@node
async def reading_node(trigger: ReadingTriggered) -> None:
    """消费一个读书触发：turn 幂等查重 → 跑阅读 agent → CAS 提交印象 + 页号（fail-soft）。

    流程：
      1. 读当前印象（拿 prior_impression / start_page / expected_ver / last_request_id）。
      2. **turn 幂等查重**：last_request_id == 本次 request_id → 已提交过、跳过（不跑 agent）。
      3. 跑阅读 agent（``run_reading_round``）。返回 None（fail-soft）→ 不提交、早返。
      4. **CAS 提交**：用读到的 ver 做 ``save_impression(expected_ver=)``。读到书尾置读完。
         CAS 落败（并发抢先）→ log、本程作废（不炸、她可重读）。
    """
    lane = trigger.lane
    persona_id = trigger.persona_id
    book_id = trigger.book_id

    current = await find_book_impression(
        lane=lane, persona_id=persona_id, book_id=book_id
    )

    # turn 幂等查重（spec 三条之一）：这一程已经提交过了 → 跳过、绝不重复跑昂贵的阅读
    # agent（durable 重投 / 整轮重试会重复递这条触发）。仿 life_wake round marker 查重。
    if current is not None and current.last_request_id == trigger.request_id:
        logger.info(
            "[reading] %s/%s book=%s request=%s already committed, skip "
            "(turn idempotent)",
            lane, persona_id, book_id, trigger.request_id,
        )
        return

    prior_impression = current.impression if current is not None else None
    start_page = current.pages_read if current is not None else 0
    # CAS 基线 ver：首次开读为 0（对齐 insert_append 的 COALESCE(MAX(ver),0) base）。
    expected_ver = current.ver if current is not None else 0

    # 跑阅读 agent（昂贵）：它往后读一程、揉出新印象。read 工具只读、无 durable mutation，
    # 整轮重放只重读、不写脏（重试安全的根）。失败 fail-soft 返回 None。
    result = await run_reading_round(
        lane=lane,
        persona_id=persona_id,
        book_id=book_id,
        book_title=trigger.book_title,
        prior_impression=prior_impression,
        start_page=start_page,
        round_id=trigger.request_id,
    )
    if result is None:
        # fail-soft：阅读 agent 失败（超时 / 抛错 / 空产出）→ 印象 / 页号都不动、不提交。
        # 这一程不算，她下次自己想读时再触发一次重读（绝不写半截脏印象）。
        logger.info(
            "[reading] %s/%s book=%s request=%s reading round failed, "
            "impression/progress untouched (she can reread)",
            lane, persona_id, book_id, trigger.request_id,
        )
        return

    # 读到书尾 → 状态置「读完」（页号已由 agent 夹到 total、不越界）；否则仍在读。
    status = STATUS_FINISHED if result.finished else STATUS_READING

    # CAS 提交（spec 三条之二/之三）：真正的 durable mutation 在这里、在 agent 返回之后
    # 做一次。expected_ver 是上面读到的当前 ver——并发 / 过期任务（拿着过时印象的重放）
    # 写入会被拒（save 返回 False）、不覆盖更新的印象、不双推进页号。request_id 落进
    # last_request_id 列，下次同一触发重投靠它 turn 幂等查重跳过。
    committed = await save_impression(
        lane=lane,
        persona_id=persona_id,
        book_id=book_id,
        impression=result.impression,
        pages_read=result.pages_read,
        status=status,
        observed_at=cst_time.now_cst_iso(),
        expected_ver=expected_ver,
        request_id=trigger.request_id,
    )
    if not committed:
        # CAS 落败 = 期间有人 append（并发 / 过期任务抢先到更新的一版）：本程作废、
        # 不炸（这正是 CAS 要保护的「过期任务不覆盖更新印象」路径）。她可重读。
        logger.info(
            "[reading] %s/%s book=%s request=%s CAS lost race "
            "(expected_ver=%d advanced), round abandoned (she can reread)",
            lane, persona_id, book_id, trigger.request_id, expected_ver,
        )
        return

    logger.info(
        "[reading] %s/%s book=%s request=%s committed impression, "
        "pages_read=%d status=%s",
        lane, persona_id, book_id, trigger.request_id, result.pages_read, status,
    )
