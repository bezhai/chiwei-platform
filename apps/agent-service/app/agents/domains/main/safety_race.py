"""Pre-safety vs main-agent race logic

并行模式下，pre-safety 和主模型同时运行。
此模块实现 token 缓冲 + race 逻辑：
- Phase 1: 缓冲 token，等 pre 结果
- Phase 2: pre 通过后，直接透传 token
"""

import asyncio
import logging
import time
from collections.abc import AsyncGenerator

from app.middleware.chat_metrics import CHAT_PIPELINE_DURATION

logger = logging.getLogger(__name__)

_STREAM_END = object()


async def buffer_until_pre(
    raw_stream: AsyncGenerator[str, None],
    pre_task: asyncio.Task,
    message_id: str,
    guard_message: str = "不想讨论这个话题呢~",
) -> AsyncGenerator[str, None]:
    """用 pre_task 结果守护一个原始 token 流。

    使用 Queue + asyncio.wait 实现 race：pre 完成后立即响应，
    不再被动等待下一个 token 到达才检查。
    """
    t_buf_start = time.monotonic()
    buffer: list[str] = []
    q: asyncio.Queue = asyncio.Queue()

    async def _drain_stream():
        try:
            async for text in raw_stream:
                await q.put(text)
        except asyncio.CancelledError:
            logger.warning(f"_drain_stream cancelled: message_id={message_id}")
            raise
        except Exception as e:
            logger.error(f"_drain_stream error: message_id={message_id}, error={e}")
            await q.put(e)
        finally:
            try:
                await q.put(_STREAM_END)
            except asyncio.CancelledError:
                # 即使被取消也要确保 STREAM_END 入队（用非 async 方式）
                q.put_nowait(_STREAM_END)
                logger.warning(f"_drain_stream STREAM_END forced via put_nowait: message_id={message_id}")

    drain_task = asyncio.create_task(_drain_stream())

    try:
        # Phase 1: Race pre vs stream tokens
        while not pre_task.done():
            get_task = asyncio.ensure_future(q.get())
            done, _ = await asyncio.wait(
                {get_task, pre_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if pre_task in done:
                pre_result = pre_task.result()
                pre_dur = time.monotonic() - t_buf_start
                CHAT_PIPELINE_DURATION.labels(stage="pre_safety").observe(pre_dur)
                logger.info(
                    "pre_safety_done",
                    extra={
                        "event": "pre_safety_done",
                        "message_id": message_id,
                        "duration_ms": round(pre_dur * 1000),
                        "blocked": pre_result["is_blocked"],
                        "buffered": len(buffer),
                    },
                )
                if pre_result["is_blocked"]:
                    logger.info(
                        f"并行模式拦截: message_id={message_id}, "
                        f"reason={pre_result['block_reason']}"
                    )
                    get_task.cancel()
                    yield guard_message
                    return
                # pre passed -> flush buffer
                for b in buffer:
                    yield b
                buffer.clear()
                # Await pending get
                item = await get_task
                if isinstance(item, Exception):
                    raise item
                if item is _STREAM_END:
                    return
                yield item
                break  # -> Phase 2

            # Token arrived, pre still running
            item = await get_task
            if isinstance(item, Exception):
                raise item
            if item is _STREAM_END:
                # Stream ended before pre -> await pre
                try:
                    pre_result = await pre_task
                except Exception as e:
                    logger.error(f"pre_task 异常: {e}")
                    for b in buffer:
                        yield b
                    return
                pre_dur = time.monotonic() - t_buf_start
                CHAT_PIPELINE_DURATION.labels(stage="pre_safety").observe(pre_dur)
                if pre_result["is_blocked"]:
                    logger.info(
                        f"并行模式拦截（流结束后）: message_id={message_id}, "
                        f"reason={pre_result['block_reason']}"
                    )
                    yield guard_message
                    return
                for b in buffer:
                    yield b
                return
            buffer.append(item)

        # Edge: pre done between loop iterations
        if buffer:
            pre_result = pre_task.result()
            if pre_result["is_blocked"]:
                logger.info(
                    f"并行模式拦截: message_id={message_id}, "
                    f"reason={pre_result['block_reason']}"
                )
                yield guard_message
                return
            for b in buffer:
                yield b
            buffer.clear()

        # Phase 2: Pre passed, stream directly from queue
        _PHASE2_TIMEOUT = 120  # 2 分钟超时防御
        logger.debug(f"_buffer_until_pre phase2 start: message_id={message_id}")
        while True:
            try:
                item = await asyncio.wait_for(q.get(), timeout=_PHASE2_TIMEOUT)
            except TimeoutError:
                logger.error(
                    f"_buffer_until_pre phase2 TIMEOUT ({_PHASE2_TIMEOUT}s): "
                    f"message_id={message_id}, drain_task.done={drain_task.done()}"
                )
                return
            if isinstance(item, Exception):
                raise item
            if item is _STREAM_END:
                logger.debug(f"_buffer_until_pre phase2 STREAM_END: message_id={message_id}")
                return
            yield item

    finally:
        if not drain_task.done():
            logger.warning(f"_buffer_until_pre finally: cancelling drain_task, message_id={message_id}")
            drain_task.cancel()
