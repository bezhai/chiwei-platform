from enum import Enum


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMMIT = "COMMIT"
    DONE = "DONE"
    FAILED = "FAILED"
