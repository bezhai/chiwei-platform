import logging
import os

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from app.config import INNER_HTTP_SECRET, MAX_TIMEOUT
from app.executor import ExecutionResult, execute

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="sandbox-worker", version=os.getenv("GIT_SHA", "dev"))


# ─── Auth ────────────────────────────────────────────────────


async def _verify_auth(request: Request) -> None:
    if not INNER_HTTP_SECRET:
        return
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != INNER_HTTP_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")


# ─── Routes ──────────────────────────────────────────────────


class ExecRequest(BaseModel):
    command: str
    skill_name: str = ""
    envs: dict[str, str] | None = None
    timeout_sec: int = 30


class ExecResponse(BaseModel):
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: float


@app.post("/exec", response_model=ExecResponse)
async def exec_command(req: ExecRequest, request: Request):
    await _verify_auth(request)

    if not req.command.strip():
        raise HTTPException(status_code=400, detail="command is required")

    timeout = min(max(req.timeout_sec, 1), MAX_TIMEOUT)

    logger.info(
        "exec: skill=%s timeout=%ds cmd=%s",
        req.skill_name or "(none)",
        timeout,
        req.command[:200],
    )

    result: ExecutionResult = await execute(
        command=req.command,
        skill_name=req.skill_name,
        timeout=timeout,
        envs=req.envs,
    )

    return ExecResponse(
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
    )


@app.get("/health")
async def health():
    return {"status": "ok"}
