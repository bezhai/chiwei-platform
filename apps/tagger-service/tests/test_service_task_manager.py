from __future__ import annotations

import asyncio

from app.service.task_manager import TaskManager
from app.service.task_store import STATUS_ACCEPTED, TaskStore


class FakeLocalQwen:
    async def infer_paths(self, paths):
        return {"rows": [{"id": path} for path in paths], "dups": []}


class FakeRemoteTagger:
    async def infer(self, paths):
        return {"rows": [{"id": path} for path in paths], "dups": []}


def build_manager(store: TaskStore, *, queue_size: int = 1) -> TaskManager:
    return TaskManager(
        store=store,
        local_qwen=FakeLocalQwen(),
        remote_tagger=FakeRemoteTagger(),
        queue_size=queue_size,
        callback_retries=0,
        callback_auth_token="token",
        callback_timeout_seconds=1,
        callback_retry_delay_seconds=1,
        local_infer_timeout_seconds=30,
        exit_on_local_timeout=False,
    )


def test_submit_persists_even_when_internal_queue_is_full(tmp_path) -> None:
    async def scenario() -> None:
        store = TaskStore(tmp_path / "tasks.sqlite3")
        store.init()
        manager = build_manager(store, queue_size=1)
        manager._queue.put_nowait("already-full")

        task_id = await manager.submit(["a.jpg"], "http://localhost/callback")

        record = store.get_task(task_id)
        assert record.status == STATUS_ACCEPTED
        assert record.paths == ["a.jpg"]

    asyncio.run(scenario())


def test_dispatcher_fills_queue_from_recoverable_sqlite_tasks_without_blocking(tmp_path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.init()
    for index in range(3):
        store.create_task(f"t{index}", [f"{index}.jpg"], "http://localhost/callback")

    manager = build_manager(store, queue_size=2)

    dispatched = manager._dispatch_recoverable_tasks()

    assert dispatched == 2
    assert manager._queue.qsize() == 2
    assert manager._queue.get_nowait() == "t0"
    assert manager._queue.get_nowait() == "t1"
