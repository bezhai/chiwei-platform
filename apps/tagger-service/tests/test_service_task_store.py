from __future__ import annotations

from app.service.task_store import (
    STATUS_ACCEPTED,
    STATUS_COMPLETED,
    STATUS_PENDING_CALLBACK,
    STATUS_RUNNING,
    TaskStore,
)


def test_task_store_lifecycle_and_recovery(tmp_path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.init()

    record = store.create_task("t1", ["a.png"], "http://localhost/callback")
    assert record.status == STATUS_ACCEPTED

    store.mark_running("t1")
    assert store.get_task("t1").status == STATUS_RUNNING
    assert [task.task_id for task in store.recoverable_tasks()] == ["t1"]

    store.mark_pending_callback("t1", {"task_id": "t1", "rows": []})
    pending = store.get_task("t1")
    assert pending.status == STATUS_PENDING_CALLBACK
    assert pending.result == {"task_id": "t1", "rows": []}

    attempts = store.record_callback_failure("t1", "boom")
    assert attempts == 1

    store.mark_completed("t1")
    assert store.get_task("t1").status == STATUS_COMPLETED
    assert store.recoverable_tasks() == []
