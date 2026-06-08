from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


STATUS_ACCEPTED = "accepted"
STATUS_RUNNING = "running"
STATUS_PENDING_CALLBACK = "pending_callback"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
RECOVERABLE_STATUSES = (STATUS_ACCEPTED, STATUS_RUNNING, STATUS_PENDING_CALLBACK)


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    status: str
    paths: list[str]
    callback_url: str
    result: dict[str, Any] | None
    attempts: int
    error: str | None
    created_at: float
    updated_at: float


class TaskStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tagger_tasks (
                    task_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    paths_json TEXT NOT NULL,
                    callback_url TEXT NOT NULL,
                    result_json TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_tagger_tasks_status "
                "ON tagger_tasks(status, updated_at)"
            )

    def create_task(self, task_id: str, paths: list[str], callback_url: str) -> TaskRecord:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tagger_tasks (
                    task_id, status, paths_json, callback_url, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (task_id, STATUS_ACCEPTED, json.dumps(paths), callback_url, now, now),
            )
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> TaskRecord:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tagger_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return _record_from_row(row)

    def mark_running(self, task_id: str) -> None:
        self._update(task_id, status=STATUS_RUNNING, error=None)

    def mark_pending_callback(self, task_id: str, result: dict[str, Any]) -> None:
        self._update(task_id, status=STATUS_PENDING_CALLBACK, result=result, error=None)

    def mark_completed(self, task_id: str) -> None:
        self._update(task_id, status=STATUS_COMPLETED, error=None)

    def mark_failed(self, task_id: str, error: str) -> None:
        self._update(task_id, status=STATUS_FAILED, error=error)

    def record_callback_failure(self, task_id: str, error: str) -> int:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE tagger_tasks
                SET attempts = attempts + 1, error = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (error, now, task_id),
            )
            row = conn.execute(
                "SELECT attempts FROM tagger_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return int(row["attempts"])

    def recoverable_tasks(self) -> list[TaskRecord]:
        placeholders = ",".join("?" for _ in RECOVERABLE_STATUSES)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM tagger_tasks WHERE status IN ({placeholders}) ORDER BY updated_at",
                RECOVERABLE_STATUSES,
            ).fetchall()
        return [_record_from_row(row) for row in rows]

    def _update(
        self,
        task_id: str,
        *,
        status: str | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        assignments = ["updated_at = ?"]
        values: list[Any] = [time.time()]
        if status is not None:
            assignments.append("status = ?")
            values.append(status)
        if result is not None:
            assignments.append("result_json = ?")
            values.append(json.dumps(result, ensure_ascii=False))
        if error is not None:
            assignments.append("error = ?")
            values.append(error)
        elif error is None and status in {STATUS_RUNNING, STATUS_PENDING_CALLBACK, STATUS_COMPLETED}:
            assignments.append("error = NULL")
        values.append(task_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE tagger_tasks SET {', '.join(assignments)} WHERE task_id = ?",
                values,
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn


def _record_from_row(row: sqlite3.Row) -> TaskRecord:
    result_raw = row["result_json"]
    return TaskRecord(
        task_id=str(row["task_id"]),
        status=str(row["status"]),
        paths=json.loads(str(row["paths_json"])),
        callback_url=str(row["callback_url"]),
        result=json.loads(result_raw) if result_raw else None,
        attempts=int(row["attempts"]),
        error=row["error"],
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )
