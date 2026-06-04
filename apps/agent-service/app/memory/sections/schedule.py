"""Schedule section — retired.

Schedules are no longer generated (the life-engine schedule pipeline was
removed in the world/life rewrite). This section is kept as a no-op so the
context composer's call site stays uniform: it always gets an empty string
back, never appends a schedule section, and never errors.
"""

from __future__ import annotations


async def build_schedule_section(*, persona_id: str) -> str:
    return ""
