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
    # APP_NAME picks the node subset this process serves. It MUST be
    # injected by PaaS per-Deployment — without it, ``Runtime`` would
    # fall back to ``DEFAULT_APP`` ("agent-service") and the worker
    # would silently come up as the wrong runtime (e.g. a vectorize-
    # worker pod running agent-service's node subset). Fail loud here.
    app_name = os.getenv("APP_NAME")
    if not app_name:
        raise RuntimeError(
            "runtime_entry requires the APP_NAME env to be set "
            "(per-Deployment injection is owned by PaaS)"
        )
    setup_logging(log_dir="/logs/agent-service", log_file=f"{app_name}.log")
    load_dataflow_graph()
    asyncio.run(Runtime(app_name=app_name).run())


if __name__ == "__main__":
    main()
