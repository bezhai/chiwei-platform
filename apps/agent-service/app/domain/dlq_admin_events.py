"""Phase 7b Gap 12: DLQ admin HTTP request/response Data classes."""
from __future__ import annotations

from typing import Annotated

from app.runtime.data import Data, Key


class DlqInspectRequest(Data):
    request_id: Annotated[str, Key]
    queue: str
    limit: int = 20
    queue_kind: str = "dlq"


class DlqInspectResponse(Data):
    request_id: Annotated[str, Key]
    rows: list[dict]


class DlqClearIdempotentRequest(Data):
    request_id: Annotated[str, Key]
    by: str
    trace_id: str | None = None
    edge_id: str | None = None
    idempotent_key: str | None = None


class DlqClearIdempotentResponse(Data):
    request_id: Annotated[str, Key]
    deleted: int = 0
    skipped_succeeded: int = 0
    error: str | None = None
    edge_id: str | None = None
    idempotent_key: str | None = None
    status_code: int = 200


class DlqDryRunRequest(Data):
    request_id: Annotated[str, Key]
    queue: str
    limit: int = 20
    queue_kind: str = "dlq"


class DlqDryRunResponse(Data):
    request_id: Annotated[str, Key]
    plan: list[dict]


class DlqRequeueRequest(Data):
    request_id: Annotated[str, Key]
    queue: str
    queue_kind: str = "dlq"
    limit: int = 20
    clear_idempotent: bool = False


class DlqRequeueResponse(Data):
    request_id: Annotated[str, Key]
    requeued: int = 0
    publish_failed: int = 0
    zombie_acked: int = 0
    status_code: int = 200
