"""Admin / public-API HTTP wiring — Phase 6 v4 Gap 1 closure.

Each wire declares a Source.http(...) input + admin node consumer; runtime
auto-registers FastAPI routes via register_http_sources(app).

All endpoints use response=True (RPC mode) to preserve the old synchronous
response shapes — clients depend on getting a JSON body back, not a 202.
"""
from app.domain.admin import (
    AdminGlimpseRequest,
    AdminLifeTickRequest,
    AdminScheduleRequest,
    AdminSearchRequest,
    AdminVoiceRequest,
    DebugGlimpseRequest,
    ScheduleCreateRequest,
    ScheduleCurrentRequest,
    ScheduleDailyRequest,
    ScheduleDeleteRequest,
    ScheduleListRequest,
)
from app.nodes.admin import (
    admin_debug_glimpse_node,
    admin_life_tick_node,
    admin_search_node,
    admin_trigger_glimpse_node,
    admin_trigger_schedule_node,
    admin_trigger_voice_node,
    create_schedule_node,
    current_schedule_node,
    daily_entries_node,
    delete_schedule_node,
    list_schedules_node,
)
from app.runtime import Source, wire

# Admin trigger endpoints — all RPC mode (preserve old sync response shape).
wire(AdminLifeTickRequest).from_(
    Source.http("/admin/trigger-life-engine-tick", response=True)
).to(admin_life_tick_node)
wire(AdminGlimpseRequest).from_(
    Source.http("/admin/trigger-glimpse", response=True)
).to(admin_trigger_glimpse_node)
wire(DebugGlimpseRequest).from_(
    Source.http("/admin/debug-glimpse", response=True)
).to(admin_debug_glimpse_node)
wire(AdminVoiceRequest).from_(
    Source.http("/admin/trigger-voice", response=True)
).to(admin_trigger_voice_node)
wire(AdminScheduleRequest).from_(
    Source.http("/admin/trigger-schedule", response=True)
).to(admin_trigger_schedule_node)
wire(AdminSearchRequest).from_(
    Source.http("/admin/search", response=True)
).to(admin_search_node)

# Schedule CRUD endpoints — all RPC mode.
wire(ScheduleListRequest).from_(
    Source.http("/api/schedule", method="GET", response=True)
).to(list_schedules_node)
wire(ScheduleCurrentRequest).from_(
    Source.http("/api/schedule/current", method="GET", response=True)
).to(current_schedule_node)
wire(ScheduleDailyRequest).from_(
    Source.http("/api/schedule/daily/{target_date}", method="GET", response=True)
).to(daily_entries_node)
wire(ScheduleCreateRequest).from_(
    Source.http("/api/schedule", response=True)
).to(create_schedule_node)
wire(ScheduleDeleteRequest).from_(
    Source.http("/api/schedule/{schedule_id}", method="DELETE", response=True)
).to(delete_schedule_node)

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
