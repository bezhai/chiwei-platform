"""The script sandbox (``sandbox-worker``), at the HTTP boundary.

``app.capabilities.sandbox.run`` posts to ``http://sandbox-worker[:port]/exec`` (whatever
``lane_router`` resolves) with the lane header; both ways a command runs go through it: her
``run_a_script`` and a guide's ``!`cmd``` preprocessing when she reads it
(``app.skills.renderer.render_skill``). Everything above the transport runs for real, the output
cap included.

The host is served on :mod:`tests.replay.harness.network`: ``POST /exec`` answers what the
scenario gave for the request's ``command`` (:meth:`Sandbox.runs`); any other command is refused.
The request goes onto the effects timeline::

    {"http": "POST sandbox-worker/exec", "lane": "coe-replay",
     "request": {"command": "...", "skill_name": "bangumi", "envs": {}, "timeout_sec": 30},
     "status": 200}
"""

from __future__ import annotations

import json

import httpx

from tests.replay.harness.network import Network, json_response

HOST = "sandbox-worker"


class Sandbox:
    def __init__(self, network: Network) -> None:
        self._network = network
        self._runs = network.answers("sandbox")
        network.serve(HOST, self._serve)

    def runs(
        self, command: str, *, stdout: str = "", stderr: str = "", exit_code: int = 0
    ) -> None:
        """What running ``command`` in the sandbox prints and exits with."""
        self._runs.give(
            command, {"exit_code": exit_code, "stdout": stdout, "stderr": stderr}
        )

    async def _serve(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST" or request.url.path != "/exec":
            self._network.refuse(request, "sandbox path not served")
        body = json.loads(request.content or b"{}")
        result = self._runs.take(body.get("command"), request)
        self._network.effects.append(
            {
                "http": f"POST {HOST}{request.url.path}",
                "lane": request.headers.get("x-ctx-lane"),
                "request": body,
                "status": 200,
            }
        )
        return json_response(request, result)
