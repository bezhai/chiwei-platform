"""The step's effects timeline, and the process being killed at a point on it.

Every boundary appends to one :class:`Timeline` in the order things happen: a write
transaction ending, a publish, a delivery and how it settled, a file written or deleted. That
order is what shows the phases of a round's end (spec decision 3).

:meth:`Timeline.kill_after` arms a kill: once an entry the predicate accepts has been appended,
the process is dead, and the next thing it tries at any boundary (a SQL statement, a publish, a
file write, a model call) raises :class:`ProcessKilled`. ``ProcessKilled`` is a
``BaseException``, so the code's own ``except Exception`` handlers do not swallow it and nothing
after the kill point reaches the outside world. Expressed on the timeline rather than on a
function name, a kill point survives the code being moved around.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

Effect = dict[str, Any]


class ProcessKilled(BaseException):
    """The process died here. Raised at the first boundary crossed after the kill point."""


class Timeline(list):
    def __init__(self) -> None:
        super().__init__()
        self._kill_after: Callable[[Effect], bool] | None = None
        self._killed_after: Effect | None = None

    def kill_after(self, matches: Callable[[Effect], bool]) -> None:
        self._kill_after = matches

    def append(self, entry: Effect) -> None:  # type: ignore[override]
        super().append(entry)
        if self._kill_after is not None and self._kill_after(entry):
            self._kill_after = None
            self._killed_after = entry

    def alive(self) -> None:
        if self._killed_after is not None:
            raise ProcessKilled("the process was killed")

    def revive(self) -> None:
        """A new process; the next kill has to be armed again."""
        self._kill_after = None
        self._killed_after = None
