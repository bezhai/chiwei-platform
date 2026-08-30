from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app.pipeline.qwen_stage import QwenVlEndpoint
from app.pipeline.run_mvp import build_stages
from app.service.callbacks import callback_url_allowed
from app.service.auth import bearer_token_allowed
from app.service.image_loader import MinioObjectReader
from app.service.inference import LocalInferenceService
from app.service.path_validation import PathValidationError, validate_basename_paths
from app.service.remote_client import RemoteTaggerClient
from app.service.runner import PersistentStageRunner
from app.service.task_manager import TaskManager
from app.service.task_store import TaskStore
from app.settings import Settings, load_settings


class SubmitRequest(BaseModel):
    paths: list[str] = Field(min_length=1)
    callback_url: str


class SubmitResponse(BaseModel):
    task_id: str
    status: str


class InferRequest(BaseModel):
    paths: list[str] = Field(min_length=1)


def _validate_paths(paths: list[str], settings: Settings) -> None:
    try:
        validate_basename_paths(paths, max_batch_paths=settings.max_batch_paths)
    except PathValidationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _require_auth(settings: Settings, authorization: str | None) -> None:
    if not settings.api_tokens:
        raise HTTPException(status_code=503, detail="tagger API auth is not configured")
    if not bearer_token_allowed(authorization, settings.api_tokens):
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


def _build_reader(settings: Settings) -> MinioObjectReader:
    if not settings.minio_access_key or not settings.minio_secret_key:
        raise RuntimeError("MINIO_ACCESS_KEY and MINIO_SECRET_KEY are required")
    endpoint = settings.minio_endpoint
    if ":" not in endpoint:
        endpoint = f"{endpoint}:{settings.minio_port}"
    return MinioObjectReader(
        endpoint=endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        bucket=settings.minio_bucket,
        secure=settings.minio_secure,
    )


def _build_qwen_endpoint(settings: Settings) -> QwenVlEndpoint:
    if not settings.qwen_base_url or not settings.qwen_model:
        raise RuntimeError("TAGGER_QWEN_BASE_URL and TAGGER_QWEN_MODEL are required for entry role")
    return QwenVlEndpoint(
        base_url=settings.qwen_base_url,
        model=settings.qwen_model,
        api_key=settings.qwen_api_key,
        timeout_seconds=settings.qwen_timeout_seconds,
        concurrency=settings.qwen_concurrency,
        max_new_tokens=settings.qwen_max_new_tokens,
        max_vision_tokens=settings.qwen_max_vision_tokens,
    )


def _build_local_inference(
    settings: Settings, *, qwen: QwenVlEndpoint | None, with_taggers: bool
) -> LocalInferenceService:
    if with_taggers and (settings.wd14_model_dir is None or settings.eva02_model_dir is None):
        raise RuntimeError("TAGGER_WD14_MODEL_DIR and TAGGER_EVA02_MODEL_DIR are required for backend role")
    stages = build_stages(
        qwen=qwen,
        with_taggers=with_taggers,
        wd14_model_dir=settings.wd14_model_dir,
        eva02_model_dir=settings.eva02_model_dir,
    )
    return LocalInferenceService(
        reader=_build_reader(settings),
        runner=PersistentStageRunner(stages),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    app.state.settings = settings
    app.state.store = TaskStore(settings.sqlite_path)
    app.state.store.init()

    role = settings.role
    if role == "entry":
        app.state.local_qwen = _build_local_inference(
            settings, qwen=_build_qwen_endpoint(settings), with_taggers=False
        )
        app.state.remote_tagger = RemoteTaggerClient(
            settings.remote_tagger_url,
            auth_token=settings.remote_auth_token,
            timeout_seconds=settings.remote_timeout_seconds,
            retries=settings.remote_retries,
        )
        app.state.task_manager = TaskManager(
            store=app.state.store,
            local_qwen=app.state.local_qwen,
            remote_tagger=app.state.remote_tagger,
            queue_size=settings.queue_size,
            callback_retries=settings.callback_retries,
            callback_auth_token=settings.callback_auth_token,
            callback_timeout_seconds=settings.callback_timeout_seconds,
            callback_retry_delay_seconds=settings.callback_retry_delay_seconds,
            local_infer_timeout_seconds=settings.local_infer_timeout_seconds,
            exit_on_local_timeout=settings.exit_on_local_timeout,
        )
        await app.state.task_manager.start()
    elif role == "backend":
        app.state.local_tagger = _build_local_inference(settings, qwen=None, with_taggers=True)
    else:
        raise RuntimeError(f"unsupported TAGGER_ROLE={role!r}, expected entry or backend")

    yield

    task_manager = getattr(app.state, "task_manager", None)
    if task_manager is not None:
        await task_manager.stop()
    for attr in ("local_qwen", "local_tagger"):
        service = getattr(app.state, attr, None)
        if service is not None:
            await service.unload()


app = FastAPI(title="tagger-service", version=os.getenv("GIT_SHA", "dev"), lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    # 进程存活探针。模型不在本进程里，所以不再有 model_ready：能不能出结果取决于上游 llama-swap，
    # 要探那个得往上游发真实推理请求，不适合挂在每次探活上。改成回报配置指向的模型名，
    # 方便运维确认这台连的是哪个模型。
    settings: Settings = app.state.settings
    payload: dict[str, Any] = {"status": "ok", "role": settings.role}
    if settings.role == "entry":
        payload["qwen_model"] = settings.qwen_model
    return payload


@app.post("/api/v1/tagger/submit", response_model=SubmitResponse)
async def submit(req: SubmitRequest, authorization: str | None = Header(default=None)) -> SubmitResponse:
    settings: Settings = app.state.settings
    _require_auth(settings, authorization)
    if settings.role != "entry":
        raise HTTPException(status_code=404, detail="submit endpoint is only enabled on entry role")
    _validate_paths(req.paths, settings)
    if not callback_url_allowed(
        req.callback_url,
        allowed_hosts=settings.callback_allowed_hosts,
        allowed_networks=settings.callback_allowed_networks,
    ):
        raise HTTPException(status_code=400, detail="callback_url is not allowed")
    task_id = await app.state.task_manager.submit(req.paths, req.callback_url)
    return SubmitResponse(task_id=task_id, status="accepted")


@app.post("/api/v1/tagger/infer")
async def infer(req: InferRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    settings: Settings = app.state.settings
    _require_auth(settings, authorization)
    if settings.role != "backend":
        raise HTTPException(status_code=404, detail="infer endpoint is only enabled on backend role")
    _validate_paths(req.paths, settings)
    return await app.state.local_tagger.infer_paths(req.paths)


@app.get("/api/v1/tagger/tasks/{task_id}")
async def get_task(task_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    settings: Settings = app.state.settings
    _require_auth(settings, authorization)
    try:
        record = app.state.store.get_task(task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="task not found")
    return {
        "task_id": record.task_id,
        "status": record.status,
        "paths": record.paths,
        "callback_url": record.callback_url,
        "result": record.result,
        "attempts": record.attempts,
        "error": record.error,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }
