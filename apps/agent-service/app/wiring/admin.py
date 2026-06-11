"""Admin / public-API HTTP wiring — Phase 6 v4 Gap 1 closure.

Each wire declares a Source.http(...) input + admin node consumer; runtime
auto-registers FastAPI routes via register_http_sources(app).

All endpoints use response=True (RPC mode) to preserve the old synchronous
response shapes — clients depend on getting a JSON body back, not a 202.
"""
from app.domain.admin import AdminSearchRequest
from app.nodes.admin import admin_search_node
from app.runtime import Source, wire

# Admin trigger endpoints — all RPC mode (preserve old sync response shape).
wire(AdminSearchRequest).from_(
    Source.http("/admin/search", response=True)
).to(admin_search_node)

# Phase 7b Gap 12: DLQ admin endpoints.
from app.domain.dlq_admin_events import (  # noqa: E402
    DlqClearIdempotentRequest,
    DlqDryRunRequest,
    DlqInspectRequest,
    DlqRequeueRequest,
)
from app.nodes.dlq_admin import (  # noqa: E402
    dlq_clear_idempotent_node,
    dlq_dry_run_node,
    dlq_inspect_node,
    dlq_requeue_node,
)

wire(DlqInspectRequest).from_(
    Source.http("/admin/dlq/inspect", method="POST", response=True)
).to(dlq_inspect_node)
wire(DlqClearIdempotentRequest).from_(
    Source.http("/admin/dlq/clear-idempotent", method="POST", response=True)
).to(dlq_clear_idempotent_node)
wire(DlqDryRunRequest).from_(
    Source.http("/admin/dlq/dry-run", method="POST", response=True)
).to(dlq_dry_run_node)
wire(DlqRequeueRequest).from_(
    Source.http("/admin/dlq/requeue", method="POST", response=True)
).to(dlq_requeue_node)
