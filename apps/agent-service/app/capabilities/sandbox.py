"""Sandbox skill execution capability — Phase 7d Gap 16.

Calls the ``sandbox-worker`` service ``/exec`` endpoint via ``HTTPClient``.
``/exec`` runs a bash command, so it is non-idempotent: ``retries=0``.
Lane and trace headers are auto-injected by ``HTTPClient``.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.capabilities.http import HTTPClient
from app.infra.config import settings

# service="sandbox-worker" → LaneRouter resolves the right lane.
# retries=0 + retry_post=0: /exec is non-idempotent (executes a command).
# Timeout = command_timeout + 15s buffer; default 30s + 15s = 45s.
_CLIENT = HTTPClient(service="sandbox-worker", timeout=45.0, retries=0)


@dataclass
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str


async def run(
    *,
    command: str,
    skill_name: str = "",
    envs: dict[str, str] | None = None,
    timeout: int = 30,
) -> SandboxResult:
    """Execute ``command`` in the sandbox; returns structured result.

    Args:
        command: bash command to run.
        skill_name: skill working-dir hint for the sandbox.
        envs: extra env vars to inject.
        timeout: command timeout (seconds).

    Raises:
        httpx.HTTPStatusError: when the sandbox-worker returns non-2xx.
    """
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if settings.inner_http_secret:
        headers["Authorization"] = f"Bearer {settings.inner_http_secret}"
    resp = await _CLIENT.post(
        "/exec",
        json={
            "command": command,
            "skill_name": skill_name,
            "envs": envs or {},
            "timeout_sec": timeout,
        },
        headers=headers,
    )
    resp.raise_for_status()
    data = resp.json()
    return SandboxResult(
        exit_code=data["exit_code"],
        stdout=data["stdout"],
        stderr=data["stderr"],
    )
