"""world 记录的人工读写接口：列目录、读一份、写一份、删一份。只给人用。

用途三个：原文灌入初始内容、人工修正、验收时读取。其他参与者不读记录，只能经通信机制问
world。接线在 :mod:`app.world.wiring`，挂在 world App 的进程里：

  GET    /admin/world/records                                   列目录
  GET    /admin/world/records/document?path=...                 读一份
  PUT    /admin/world/records/document  {path, text, fingerprint?}  写一份
  DELETE /admin/world/records/document?path=...&fingerprint=...     删一份

四条跟通信机制的人工入口做同样的检查：要内网凭据、请求要去的泳道不是这个进程的泳道就一步
都不做、每个回答都带执行它的进程所在的泳道。从开发机过来走 monitor-dashboard 的
``/dashboard/api/ops/world/records*`` 转发，那一侧认 PAAS_TOKEN、落审计、把调用者作为
``X-Operator`` 带过来；这里写和删各记一行日志，带上操作人。

写和删跟主 agent 的工具走同一条规矩（:mod:`app.world.records`）：新建不带指纹，改写和删除
带上它现在的指纹，对不上就 409、什么都不动。
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any

from fastapi import HTTPException
from pydantic import Field

from app.api.middleware import get_header_var
from app.runtime import Data, Key, node
from app.runtime.lane_policy import current_deployment_lane
from app.world import records
from app.world.volume import VolumeUnavailable, WriterLockNotHeld

logger = logging.getLogger(__name__)


def _new_request_id() -> str:
    return uuid.uuid4().hex


class RecordListRequest(Data):
    request_id: Annotated[str, Key] = Field(default_factory=_new_request_id)

    class Meta:
        transient = True


class RecordReadRequest(Data):
    request_id: Annotated[str, Key] = Field(default_factory=_new_request_id)
    path: str

    class Meta:
        transient = True


class RecordWriteRequest(Data):
    request_id: Annotated[str, Key] = Field(default_factory=_new_request_id)
    path: str
    text: str
    # 改写已有的一份时是它现在的指纹；新建时不给。
    fingerprint: str | None = None

    class Meta:
        transient = True


class RecordDeleteRequest(Data):
    request_id: Annotated[str, Key] = Field(default_factory=_new_request_id)
    path: str
    fingerprint: str

    class Meta:
        transient = True


class RecordListResponse(Data):
    lane: Annotated[str, Key]
    records: list[dict[str, Any]]

    class Meta:
        transient = True


class RecordReadResponse(Data):
    lane: str
    path: Annotated[str, Key]
    text: str
    fingerprint: str
    updated_at: str

    class Meta:
        transient = True


class RecordWriteResponse(Data):
    lane: str
    path: Annotated[str, Key]
    fingerprint: str
    previous_fingerprint: str | None
    created: bool

    class Meta:
        transient = True


class RecordDeleteResponse(Data):
    lane: str
    path: Annotated[str, Key]
    fingerprint: str

    class Meta:
        transient = True


def _lane() -> str:
    return current_deployment_lane() or "prod"


@contextmanager
def _as_http_errors() -> Iterator[None]:
    """把记录的异常翻成 HTTP：路径或正文不对 400、没有 404、指纹对不上 409、没有卷或者
    这个进程没拿着写锁 503。"""
    try:
        yield
    except (records.InvalidRecordPath, records.InvalidRecordText) as exc:
        raise HTTPException(400, detail={"lane": _lane(), "message": str(exc)}) from exc
    except records.RecordNotFound as exc:
        raise HTTPException(404, detail={"lane": _lane(), "message": str(exc)}) from exc
    except records.RecordConflict as exc:
        raise HTTPException(409, detail={"lane": _lane(), "message": str(exc)}) from exc
    except (VolumeUnavailable, WriterLockNotHeld) as exc:
        raise HTTPException(503, detail={"lane": _lane(), "message": str(exc)}) from exc


def _operator() -> str:
    return get_header_var("operator") or "unknown"


@node
async def record_listing_node(req: RecordListRequest) -> RecordListResponse:
    with _as_http_errors():
        entries = records.listing()
    return RecordListResponse(
        lane=_lane(),
        records=[
            {
                "path": e.path,
                "chars": e.chars,
                "updated_at": e.updated_at.isoformat(),
                "fingerprint": e.fingerprint,
            }
            for e in entries
        ],
    )


@node
async def record_read_node(req: RecordReadRequest) -> RecordReadResponse:
    with _as_http_errors():
        record = records.read(req.path)
    return RecordReadResponse(
        lane=_lane(),
        path=record.path,
        text=record.text,
        fingerprint=record.fingerprint,
        updated_at=record.updated_at.isoformat(),
    )


@node
async def record_write_node(req: RecordWriteRequest) -> RecordWriteResponse:
    with _as_http_errors():
        written = records.write(req.path, req.text, expected=req.fingerprint)
    logger.info(
        "world: operator %s wrote record %s (%s -> %s)",
        _operator(),
        written.path,
        req.fingerprint or "new",
        written.fingerprint,
    )
    return RecordWriteResponse(
        lane=_lane(),
        path=written.path,
        fingerprint=written.fingerprint,
        previous_fingerprint=req.fingerprint,
        created=req.fingerprint is None,
    )


@node
async def record_delete_node(req: RecordDeleteRequest) -> RecordDeleteResponse:
    with _as_http_errors():
        records.delete(req.path, expected=req.fingerprint)
    logger.info(
        "world: operator %s deleted record %s (%s)", _operator(), req.path, req.fingerprint
    )
    return RecordDeleteResponse(lane=_lane(), path=req.path, fingerprint=req.fingerprint)
