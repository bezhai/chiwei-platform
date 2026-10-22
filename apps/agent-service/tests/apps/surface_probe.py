"""Run in a fresh process by ``test_surface.py``: start ``app.main:app`` and report what it exposes.

``python -m tests.apps.surface_probe <app>`` prints one JSON line. It imports nothing at module
level but the standard library, so the only app code in the process is what ``app.main`` and the
lifespan load for ``APP_NAME``.

Auth is probed by behaviour, not read from the code. Every request carries an extra field
``__probe__`` (query string, and the JSON body for POST and PUT); request Data forbids extra
fields, so a request that gets past the guards fails validation and the handler never runs.

* Without a credential, from the process's own lane: ``401`` means the route requires the inner
  secret.
* With the credential, from another lane (``x-ctx-lane``): ``409`` means the route requires the
  request's lane to match the process's.
* A refusal whose ``detail`` is an object carrying ``lane`` means the route answers with the lane
  it ran in.
"""

from __future__ import annotations

import json
import sys

# The lane the probed process runs in, a lane it does not, and its inner secret.
LANE = "coe-surface"
ELSEWHERE = "coe-elsewhere"
SECRET = "probe"


def _detail(response):
    if response.status_code < 400:
        return None
    try:
        return response.json().get("detail")
    except ValueError:
        return None


def _requirements(client, method: str, path: str) -> list[str]:
    """What ``method path`` requires before it validates the request."""
    params = {"__probe__": "1"}
    body = {"__probe__": 1} if method in {"POST", "PUT"} else None

    stranger = client.request(
        method, path, params=params, json=body, headers={"x-ctx-lane": LANE}
    )
    elsewhere = client.request(
        method,
        path,
        params=params,
        json=body,
        headers={"authorization": f"Bearer {SECRET}", "x-ctx-lane": ELSEWHERE},
    )
    for response in (stranger, elsewhere):
        # Anything but a refusal, a validation failure or a plain answer means the probe reached
        # a handler or broke something; say what came back rather than classify it.
        if response.status_code not in {200, 401, 409, 422}:
            raise AssertionError(
                f"{method} {path}: unexpected {response.status_code} {response.text[:300]}"
            )

    required = []
    if stranger.status_code == 401:
        required.append("inner_secret")
    if elsewhere.status_code == 409:
        required.append("lane_match")
    detail = _detail(stranger)
    if isinstance(detail, dict) and "lane" in detail:
        required.append("answers_with_lane")
    return required


def probe() -> dict:
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        routes = sorted(
            (method, route.path)
            for route in app.routes
            for method in getattr(route, "methods", None) or ()
        )
        requirements = {
            f"{method} {path}": _requirements(client, method, path) for method, path in routes
        }
        metrics = client.get("/metrics")
    return {
        "routes": requirements,
        "metrics": {
            "status": metrics.status_code,
            "content_type": metrics.headers.get("content-type", ""),
        },
    }


if __name__ == "__main__":
    from unittest.mock import MagicMock, patch

    # app.main sets up file logging under /logs at import.
    patch("inner_shared.logger.setup_logging", MagicMock()).start()
    report = probe()
    sys.stdout.write(json.dumps(report) + "\n")
