"""Unified worker entry. Reads ``APP_NAME`` from env, boots :class:`Runtime`.

The ``import app.wiring`` and ``import app.deployment`` lines are side-
effect imports — they trigger ``wire(...)`` calls and ``bind(...)``
calls that register the graph before Runtime reads it. Without these
imports, Runtime starts with an empty graph.
"""

from __future__ import annotations

import asyncio
import os

from inner_shared.logger import setup_logging

import app.deployment  # noqa: F401 — side-effect: register node -> app bindings
import app.wiring  # noqa: F401 — side-effect: register wires

from app.runtime.engine import Runtime


def main() -> None:
    app_name = os.getenv("APP_NAME", "runtime")
    setup_logging(log_dir="/logs/agent-service", log_file=f"{app_name}.log")
    asyncio.run(Runtime().run())


if __name__ == "__main__":
    main()
