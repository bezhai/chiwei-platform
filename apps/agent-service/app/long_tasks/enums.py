from enum import StrEnum


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMMIT = "COMMIT"
    DONE = "DONE"
    FAILED = "FAILED"
