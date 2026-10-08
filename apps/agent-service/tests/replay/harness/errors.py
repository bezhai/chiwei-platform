"""How an exception is written into a baseline."""

from __future__ import annotations


def describe_error(exc: BaseException) -> str:
    """``Type: first line of the message``. Later lines are driver boilerplate (SQLAlchemy
    appends the statement and a version-specific help URL) that would make a baseline depend on
    library versions."""
    lines = str(exc).splitlines()
    return f"{type(exc).__name__}: {lines[0]}" if lines else type(exc).__name__
