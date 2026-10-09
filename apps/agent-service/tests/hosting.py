"""Starting an app on the plugin host inside the tests: one way, shared by every suite.

Production starts an app with ``Host.for_app(APP_NAME).start(...)`` in ``app.main``'s lifespan.
The tests start the same host, the app's real manifest or a few of its plugins, with only the
phases they need:

* :func:`start_without_io`: every plugin's setup and the graph compiled; no database, no broker,
  no clock, no background task. ``http=`` puts the routes on a FastAPI app. The ``app_host``
  fixture (``tests/conftest.py``) starts hosts this way and stops them at teardown.
* :func:`in_a_fresh_process`: the same start in a new interpreter, for what only a fresh import
  state shows (which Data classes register, which modules an app loads).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]

_NO_IO = {"http": None, "schema": False, "mq": False, "clocks": False, "tasks": False}

# What ``app.main`` does before the lifespan starts the host, then the start without IO.
# ``setup_logging`` is patched: it would create ``/logs`` on import of ``app.main``.
_STARTED = (
    "import asyncio\n"
    "from unittest.mock import MagicMock, patch\n"
    "patch('inner_shared.logger.setup_logging', MagicMock()).start()\n"
    "import app.main\n"
    "from app.host import Host\n"
    "host = Host.for_app({app!r})\n"
    "asyncio.run(host.start(http=None, schema=False, mq=False, clocks=False, tasks=False))\n"
)


async def start_without_io(host, **flags) -> None:
    """Start ``host`` with every IO phase off unless a flag turns it on (``http=app`` binds the
    routes on ``app``)."""
    await host.start(**{**_NO_IO, **flags})


def in_a_fresh_process(
    app: str, code: str, *, lane: str | None = None, timeout: float = 180
) -> str:
    """Start ``app``'s host in a new interpreter as :func:`start_without_io` does, after importing
    ``app.main``; then run ``code`` with the started host bound to ``host``. Returns stdout.

    ``APP_NAME`` is ``app``; ``LANE`` is ``lane``, unset when ``None`` (prod).
    """
    env = {k: v for k, v in os.environ.items() if k != "LANE"}
    env["APP_NAME"] = app
    if lane is not None:
        env["LANE"] = lane
    proc = subprocess.run(
        [sys.executable, "-c", _STARTED.format(app=app) + code],
        cwd=SERVICE_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout
