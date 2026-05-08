"""DRILL ONLY — Phase 7a verification surfaces.

Drills covered:

  Drill 2/3/4: retry transport + lease take-over + history backfill
    POST /admin/_drill_phase7a/trigger {"drill_id": "<id>"}
      -> drill_trigger_node emits DrillFailingRequest
      -> wire().durable().retry(n=3, base=200, max=2000)
      -> drill_failing_node always raises -> attempts 1->2->3 -> DLQ

  Drill 5: emit_delayed durable + best_effort (Gap 9)
    POST /admin/_drill_phase7a/trigger-delayed
      {"drill_id": "<id>", "delay_ms": 5000, "durability": "durable"}
      -> drill_trigger_delayed_node calls emit_delayed(DrillEchoRequest)
      -> [durable] runtime publishes envelope to
         runtime_delayed_trigger_agent-service_dev-phase7a with x-delay
         -> after delay_ms, internal trigger consumer unwraps envelope
         -> bind_context(origin_trace_id) -> emit(DrillEchoRequest)
         -> drill_echo_node logs success (no raise)
      -> [best_effort] in-process asyncio.sleep + emit; lost on restart

Revert this file (and the import in wiring/__init__.py) before /ship.
The data_drill_failing_request and data_drill_echo_request tables
created by the migrator can be dropped via /ops-db submit DROP TABLE.
"""

from __future__ import annotations

import logging
from typing import Annotated

from app.runtime import Source, bind, wire
from app.runtime.data import Data, Key
from app.runtime.emit import emit, emit_delayed
from app.runtime.node import node

logger = logging.getLogger(__name__)


class DrillTriggerRequest(Data):
    drill_id: Annotated[str, Key]

    class Meta:
        transient = True


class DrillFailingRequest(Data):
    drill_id: Annotated[str, Key]


@node
async def drill_trigger_node(req: DrillTriggerRequest):
    # @node forbids non-Data return annotations; admin nodes throughout
    # the codebase rely on the no-annotation form so RPC mode can return
    # plain dicts. We follow the same convention here.
    await emit(DrillFailingRequest(drill_id=req.drill_id))
    return {"emitted": req.drill_id}


@node
async def drill_failing_node(req: DrillFailingRequest) -> None:
    raise RuntimeError(
        f"drill phase7a: intentional failure for drill_id={req.drill_id}"
    )


# --- Drill 5: emit_delayed durable + best_effort ---


class DrillDelayedTriggerRequest(Data):
    drill_id: Annotated[str, Key]
    delay_ms: int = 5000
    durability: str = "durable"

    class Meta:
        transient = True


class DrillEchoRequest(Data):
    drill_id: Annotated[str, Key]


@node
async def drill_trigger_delayed_node(req: DrillDelayedTriggerRequest):
    # Calls emit_delayed; on durable=durable the envelope lands in the
    # runtime trigger queue with x-delay; the runtime's internal
    # consumer rebuilds Data + emit() at fire time.
    await emit_delayed(
        DrillEchoRequest(drill_id=req.drill_id),
        delay_ms=req.delay_ms,
        durability=req.durability,
    )
    return {
        "emitted": req.drill_id,
        "delay_ms": req.delay_ms,
        "durability": req.durability,
    }


@node
async def drill_echo_node(req: DrillEchoRequest) -> None:
    # Succeeds quietly so the inflight row terminates at state=succeeded
    # and we can observe trace_id propagation across the delayed boundary.
    logger.info("drill_phase7a echo fired: drill_id=%s", req.drill_id)


bind(drill_failing_node).to_app("agent-service")
bind(drill_echo_node).to_app("agent-service")


wire(DrillTriggerRequest).from_(
    Source.http("/admin/_drill_phase7a/trigger", response=True)
).to(drill_trigger_node)

wire(DrillFailingRequest).to(drill_failing_node).durable().retry(
    n=3, backoff="exponential", base_delay_ms=200, max_delay_ms=2000
)

wire(DrillDelayedTriggerRequest).from_(
    Source.http("/admin/_drill_phase7a/trigger-delayed", response=True)
).to(drill_trigger_delayed_node)

wire(DrillEchoRequest).to(drill_echo_node).durable()
