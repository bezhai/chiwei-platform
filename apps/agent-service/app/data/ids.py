"""Short primary key generators — 12-char hex uuid with an alphabetic prefix.

Used by v4 memory tables (Fragment, AbstractMemory, MemoryEdge, Note, ScheduleRevision)
and anywhere else we need a compact, prefix-tagged row id.
"""

from __future__ import annotations

import uuid


def new_id(prefix: str) -> str:
    """Generate a new id of the form ``{prefix}_{12-hex-chars}``.

    Example: ``new_id("a")`` -> ``"a_0f3c9e1b2a5d"``.
    """
    return f"{prefix}_{uuid.uuid4().hex[:12]}"
