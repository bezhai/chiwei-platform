"""Unified worker entry. Reads ``APP_NAME`` from env, boots :class:`Runtime`.

Calls ``load_dataflow_graph()`` to register wiring/deployment side-
effects + validate, the same hook the FastAPI main process uses. This
keeps every process on the same boot contract — see
``app/runtime/bootstrap.py``.
"""

from __future__ import annotations

import asyncio
import os

from inner_shared.logger import setup_logging

from app.runtime.bootstrap import load_dataflow_graph
from app.runtime.engine import Runtime


def main() -> None:
    app_name = os.getenv("APP_NAME", "runtime")
    setup_logging(log_dir="/logs/agent-service", log_file=f"{app_name}.log")
    load_dataflow_graph()
    asyncio.run(Runtime().run())


if __name__ == "__main__":
    main()
