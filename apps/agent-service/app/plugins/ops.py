"""Ops: search and the dead-letter tools, as admin routes.

  POST /admin/search                  admin_search_node
  POST /admin/dlq/inspect             dlq_inspect_node
  POST /admin/dlq/clear-idempotent    dlq_clear_idempotent_node
  POST /admin/dlq/dry-run             dlq_dry_run_node
  POST /admin/dlq/requeue             dlq_requeue_node

None of them asks for the inner credential or checks the lane: they never did, and
``tests/wiring/test_ops_routes_baseline.py`` pins their responses byte for byte.
"""
from __future__ import annotations

from app.domain.admin import AdminSearchRequest
from app.domain.dlq_admin_events import (
    DlqClearIdempotentRequest,
    DlqDryRunRequest,
    DlqInspectRequest,
    DlqRequeueRequest,
)
from app.host import Context, Plugin
from app.nodes.admin import admin_search_node
from app.nodes.dlq_admin import (
    dlq_clear_idempotent_node,
    dlq_dry_run_node,
    dlq_inspect_node,
    dlq_requeue_node,
)


def setup(ctx: Context) -> None:
    ctx.route("POST", "/admin/search", AdminSearchRequest, admin_search_node)
    ctx.route("POST", "/admin/dlq/inspect", DlqInspectRequest, dlq_inspect_node)
    ctx.route(
        "POST", "/admin/dlq/clear-idempotent", DlqClearIdempotentRequest, dlq_clear_idempotent_node
    )
    ctx.route("POST", "/admin/dlq/dry-run", DlqDryRunRequest, dlq_dry_run_node)
    ctx.route("POST", "/admin/dlq/requeue", DlqRequeueRequest, dlq_requeue_node)


PLUGIN = Plugin(name="ops", setup=setup)
