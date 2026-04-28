"""Pre-safety chat gate — local Future waiter + run_pre_safety_via_graph.

Phase 2 §3.4：chat pipeline 通过这个 module 把 pre-check 控制面接进 graph。
``run_pre_safety_via_graph`` 是给 chat pipeline 调的统一入口；返回 verdict
是 ``PreSafetyVerdict``，跟 ``_buffer_until_pre`` 的 race 模型对齐。

实现要点：
1. ``emit()`` in-process 是同步 await 整链路（节点 -> 装饰器自动 emit verdict
   -> resolve_pre_safety_waiter -> set future）。节点卡住时 ``await emit`` 也卡住，
   所以必须把 emit 包成独立 task，让超时检查跑在调用方协程上。
2. 用 ``asyncio.wait({fut, emit_task}, FIRST_COMPLETED)`` —— emit_task 早失败
   立即 fail-open，fut 先完成直接拿 verdict。
3. ``completed`` 标记区分 3 条退出：
   - 拿到 verdict 才置 True（finally 不 cancel emit_task）
   - timeout / emit 早失败 / 外层 cancel：completed=False，finally 一律
     cancel emit_task + suppress CancelledError；CancelledError 再
     propagate 给外层
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress

from app.domain.safety import PreSafetyRequest, PreSafetyVerdict
from app.runtime.emit import emit

logger = logging.getLogger(__name__)

# 节点内部超时 20s（``_run_pre_audit`` ceiling），加 1s 缓冲
_PRE_SAFETY_TIMEOUT_SECONDS: float = 21.0

_waiters: dict[str, asyncio.Future[PreSafetyVerdict]] = {}


def register(pre_request_id: str) -> asyncio.Future[PreSafetyVerdict]:
    fut: asyncio.Future[PreSafetyVerdict] = (
        asyncio.get_running_loop().create_future()
    )
    _waiters[pre_request_id] = fut
    return fut


def resolve(verdict: PreSafetyVerdict) -> None:
    fut = _waiters.get(verdict.pre_request_id)
    if fut is None or fut.done():
        return  # caller 已超时清理 / 不存在 / 已 cancel —— 安全无操作
    fut.set_result(verdict)


def cleanup(pre_request_id: str) -> None:
    _waiters.pop(pre_request_id, None)


async def run_pre_safety_via_graph(
    message_id: str, content: str, persona_id: str
) -> PreSafetyVerdict:
    """Chat pipeline 调入口：emit + 等 verdict + fail-open."""
    pre_request_id = str(uuid.uuid4())
    fut = register(pre_request_id)
    emit_task: asyncio.Task = asyncio.create_task(
        emit(PreSafetyRequest(
            pre_request_id=pre_request_id,
            message_id=message_id,
            message_content=content,
            persona_id=persona_id,
        ))
    )

    completed = False
    try:
        done, _pending = await asyncio.wait(
            {fut, emit_task},
            timeout=_PRE_SAFETY_TIMEOUT_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            logger.warning("pre safety timeout: pre_request_id=%s", pre_request_id)
        elif fut in done:
            if not emit_task.done():
                await emit_task
            elif emit_task.exception():
                logger.error(
                    "pre safety emit failed after verdict: pre_request_id=%s, error=%s",
                    pre_request_id, emit_task.exception(),
                )
            result = fut.result()
            completed = True
            return result
        else:
            assert emit_task.done()
            logger.warning(
                "pre safety emit failed before verdict: pre_request_id=%s, error=%s",
                pre_request_id, emit_task.exception(),
            )
    finally:
        if not completed and not emit_task.done():
            emit_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await emit_task
        cleanup(pre_request_id)

    return PreSafetyVerdict(
        pre_request_id=pre_request_id, message_id=message_id, is_blocked=False
    )
