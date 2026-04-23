"""Unified worker entry. Reads ``APP_NAME`` from env, boots :class:`Runtime`.

Phase 1 will add ``import app.wiring`` + ``import app.deployment`` here to
trigger ``@node`` / ``wire()`` / ``bind()`` registrations before
``Runtime().run()``. Until those modules exist, this entry script boots
an empty runtime — useful only for smoke tests.
"""

from __future__ import annotations

import asyncio

from app.runtime.engine import Runtime


def main() -> None:
    asyncio.run(Runtime().run())


if __name__ == "__main__":
    main()
