from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import uuid
from typing import Any

from app.service.callbacks import post_callback
from app.service.inference import LocalInferenceService
from app.service.remote_client import RemoteTaggerClient
from app.service.results import dedup_paths, merge_rows_for_paths
from app.service.task_store import STATUS_PENDING_CALLBACK, TaskRecord, TaskStore

logger = logging.getLogger(__name__)


class TaskManager:
    def __init__(
        self,
        *,
        store: TaskStore,
        local_qwen: LocalInferenceService,
        remote_tagger: RemoteTaggerClient,
        queue_size: int,
        callback_retries: int,
        callback_auth_token: str,
        callback_timeout_seconds: float,
        callback_retry_delay_seconds: float,
        local_infer_timeout_seconds: float,
        exit_on_local_timeout: bool,
    ) -> None:
        self._store = store
        self._local_qwen = local_qwen
        self._remote_tagger = remote_tagger
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=queue_size)
        self._callback_retries = callback_retries
        self._callback_auth_token = callback_auth_token
        self._callback_timeout_seconds = callback_timeout_seconds
        self._callback_retry_delay_seconds = callback_retry_delay_seconds
        self._local_infer_timeout_seconds = local_infer_timeout_seconds
        self._exit_on_local_timeout = exit_on_local_timeout
        self._worker: asyncio.Task[None] | None = None
        self._dispatcher: asyncio.Task[None] | None = None
        self._dispatch_event: asyncio.Event | None = None
        self._queued_or_running: set[str] = set()
        self._delayed_task_ids: set[str] = set()
        self._delayed_requeues: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        self._dispatch_event = asyncio.Event()
        self._worker = asyncio.create_task(self._run_loop())
        self._dispatcher = asyncio.create_task(self._dispatch_loop())
        self._notify_dispatcher()

    async def stop(self) -> None:
        for task in (self._dispatcher, self._worker):
            if task is None:
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for task in self._delayed_requeues:
            task.cancel()
        if self._delayed_requeues:
            await asyncio.gather(*self._delayed_requeues, return_exceptions=True)
        self._queued_or_running.clear()
        self._delayed_task_ids.clear()

    async def submit(self, paths: list[str], callback_url: str) -> str:
        task_id = uuid.uuid4().hex
        self._store.create_task(task_id, paths, callback_url)
        self._notify_dispatcher()
        return task_id

    async def _dispatch_loop(self) -> None:
        while True:
            dispatched = self._dispatch_recoverable_tasks()
            if dispatched == 0:
                await self._wait_for_dispatch_signal()

    async def _wait_for_dispatch_signal(self) -> None:
        if self._dispatch_event is None:
            await asyncio.sleep(1)
            return
        try:
            await asyncio.wait_for(self._dispatch_event.wait(), timeout=1)
        except asyncio.TimeoutError:
            return
        finally:
            self._dispatch_event.clear()

    def _dispatch_recoverable_tasks(self) -> int:
        dispatched = 0
        for task in self._store.recoverable_tasks():
            if task.task_id in self._queued_or_running:
                continue
            try:
                self._queue.put_nowait(task.task_id)
            except asyncio.QueueFull:
                break
            self._queued_or_running.add(task.task_id)
            dispatched += 1
        return dispatched

    def _notify_dispatcher(self) -> None:
        if self._dispatch_event is not None:
            self._dispatch_event.set()

    async def _run_loop(self) -> None:
        while True:
            task_id = await self._queue.get()
            try:
                await self._run_task(task_id)
            except Exception:
                logger.exception("tagger task failed: %s", task_id)
                self._store.mark_failed(task_id, "internal task failure")
            finally:
                self._queue.task_done()
                if task_id not in self._delayed_task_ids:
                    self._queued_or_running.discard(task_id)
                    self._notify_dispatcher()

    async def _run_task(self, task_id: str) -> None:
        task = self._store.get_task(task_id)
        if task.status == STATUS_PENDING_CALLBACK and task.result is not None:
            await self._deliver_callback(task)
            return

        self._store.mark_running(task_id)
        unique_paths, dups = dedup_paths(task.paths)
        local_future = asyncio.create_task(self._local_qwen.infer_paths(unique_paths))
        remote_future = asyncio.create_task(self._remote_tagger.infer(unique_paths))
        local_result = await self._await_local_result(task_id, local_future, remote_future)
        remote_result = await remote_future
        rows = merge_rows_for_paths(
            unique_paths,
            remote_result.get("rows", []),
            local_result.get("rows", []),
        )
        payload: dict[str, Any] = {
            "task_id": task_id,
            "status": "completed",
            "rows": rows,
            "dups": [*dups, *local_result.get("dups", []), *remote_result.get("dups", [])],
        }
        self._store.mark_pending_callback(task_id, payload)
        await self._deliver_callback(self._store.get_task(task_id))

    async def _deliver_callback(self, task: TaskRecord) -> None:
        if task.result is None:
            self._store.mark_failed(task.task_id, "missing result for callback")
            return
        try:
            await post_callback(
                task.callback_url,
                task.result,
                auth_token=self._callback_auth_token,
                timeout_seconds=self._callback_timeout_seconds,
            )
        except Exception as exc:
            attempts = self._store.record_callback_failure(
                task.task_id,
                f"{type(exc).__name__}: {exc}",
            )
            if attempts > self._callback_retries:
                self._store.mark_failed(task.task_id, "callback retry exhausted")
                return
            self._schedule_requeue(task.task_id)
            return
        self._store.mark_completed(task.task_id)

    async def _await_local_result(
        self,
        task_id: str,
        local_future: asyncio.Task[dict[str, Any]],
        remote_future: asyncio.Task[dict[str, Any]],
    ) -> dict[str, Any]:
        if self._local_infer_timeout_seconds <= 0:
            return await local_future
        try:
            return await asyncio.wait_for(local_future, timeout=self._local_infer_timeout_seconds)
        except asyncio.TimeoutError:
            remote_future.cancel()
            logger.critical(
                "local qwen inference timed out for task %s after %.1fs; forcing process restart=%s",
                task_id,
                self._local_infer_timeout_seconds,
                self._exit_on_local_timeout,
            )
            if self._exit_on_local_timeout:
                os._exit(1)
            raise

    def _schedule_requeue(self, task_id: str) -> None:
        self._delayed_task_ids.add(task_id)
        task = asyncio.create_task(self._requeue_later(task_id))
        self._delayed_requeues.add(task)
        task.add_done_callback(self._delayed_requeues.discard)

    async def _requeue_later(self, task_id: str) -> None:
        try:
            await asyncio.sleep(self._callback_retry_delay_seconds)
            await self._queue.put(task_id)
        finally:
            self._delayed_task_ids.discard(task_id)
