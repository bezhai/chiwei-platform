from __future__ import annotations

import asyncio

import pytest

from app.service.task_manager import TaskManager
from app.service.task_store import STATUS_ACCEPTED, TaskStore


class FakeLocalQwen:
    def __init__(self, delay: float = 0.0) -> None:
        self._delay = delay

    async def infer_paths(self, paths):
        await asyncio.sleep(self._delay)
        return {"rows": [{"id": path} for path in paths], "dups": []}


class FakeRemoteTagger:
    def __init__(self, delay: float = 0.0) -> None:
        self._delay = delay

    async def infer(self, paths):
        await asyncio.sleep(self._delay)
        return {"rows": [{"id": path} for path in paths], "dups": []}


def build_manager(
    store: TaskStore,
    *,
    queue_size: int = 1,
    local_qwen: FakeLocalQwen | None = None,
    remote_tagger: FakeRemoteTagger | None = None,
    local_infer_timeout_seconds: float = 30,
) -> TaskManager:
    return TaskManager(
        store=store,
        local_qwen=local_qwen or FakeLocalQwen(),
        remote_tagger=remote_tagger or FakeRemoteTagger(),
        queue_size=queue_size,
        callback_retries=0,
        callback_auth_token="token",
        callback_timeout_seconds=1,
        callback_retry_delay_seconds=1,
        local_infer_timeout_seconds=local_infer_timeout_seconds,
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


def test_local_inference_inside_the_batch_budget_returns_normally(tmp_path) -> None:
    async def scenario() -> None:
        store = TaskStore(tmp_path / "tasks.sqlite3")
        store.init()
        manager = build_manager(
            store,
            local_qwen=FakeLocalQwen(delay=0.02),
            local_infer_timeout_seconds=5,
        )
        local = asyncio.create_task(manager._local_qwen.infer_paths(["a.jpg"]))
        remote = asyncio.create_task(manager._remote_tagger.infer(["a.jpg"]))

        result = await manager._await_local_result("t", local, remote)

        assert result["rows"] == [{"id": "a.jpg"}]
        assert not remote.cancelled()
        await remote

    asyncio.run(scenario())


def test_local_inference_past_the_batch_budget_times_out_and_cancels_the_remote_call(
    tmp_path,
) -> None:
    # 整批超时是最后一道闸：exit_on_local_timeout=false 时抛 TimeoutError，并且不能把 98 那边的
    # 远程调用晾着（生产里 exit_on_local_timeout=true 会 os._exit(1) 让 systemd 重启进程）
    async def scenario() -> None:
        store = TaskStore(tmp_path / "tasks.sqlite3")
        store.init()
        manager = build_manager(
            store,
            local_qwen=FakeLocalQwen(delay=5),
            remote_tagger=FakeRemoteTagger(delay=5),
            local_infer_timeout_seconds=0.05,
        )
        local = asyncio.create_task(manager._local_qwen.infer_paths(["a.jpg"]))
        remote = asyncio.create_task(manager._remote_tagger.infer(["a.jpg"]))

        with pytest.raises(asyncio.TimeoutError):
            await manager._await_local_result("t", local, remote)

        await asyncio.gather(local, remote, return_exceptions=True)
        assert local.cancelled()
        assert remote.cancelled()

    asyncio.run(scenario())


def test_zero_local_infer_timeout_disables_the_batch_deadline(tmp_path) -> None:
    async def scenario() -> None:
        store = TaskStore(tmp_path / "tasks.sqlite3")
        store.init()
        manager = build_manager(
            store,
            local_qwen=FakeLocalQwen(delay=0.05),
            local_infer_timeout_seconds=0,
        )
        local = asyncio.create_task(manager._local_qwen.infer_paths(["a.jpg"]))
        remote = asyncio.create_task(manager._remote_tagger.infer(["a.jpg"]))

        result = await manager._await_local_result("t", local, remote)

        assert result["rows"] == [{"id": "a.jpg"}]
        await remote

    asyncio.run(scenario())
