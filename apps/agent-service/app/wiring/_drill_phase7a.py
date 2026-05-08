"""DRILL ONLY — Phase 7a Gap 7.2 retry transport verification.

Provides a controlled failing durable consumer so we can observe the
retry state machine on dev-phase7a:

  POST /admin/_drill_phase7a/trigger {"drill_id": "<id>"}
    -> drill_trigger_node emits DrillFailingRequest(drill_id)
    -> wire().durable().retry(n=3, base=200, max=2000) publishes to
       durable_drill_failing_request_drill_failing_node queue
    -> drill_failing_node always raises RuntimeError
    -> retry transport republishes with x-delay header, increments
       runtime_inflight.attempts, until attempts >= 3 -> DLQ

Revert this file (and the import in wiring/__init__.py) before /ship.
The data_drill_failing_request table created by the migrator can be
dropped via /ops-db submit DROP TABLE after the drill.
"""

from __future__ import annotations

from typing import Annotated

from app.runtime import Source, bind, wire
from app.runtime.data import Data, Key
from app.runtime.emit import emit
from app.runtime.node import node


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


bind(drill_failing_node).to_app("agent-service")


wire(DrillTriggerRequest).from_(
    Source.http("/admin/_drill_phase7a/trigger", response=True)
).to(drill_trigger_node)

wire(DrillFailingRequest).to(drill_failing_node).durable().retry(
    n=3, backoff="exponential", base_delay_ms=200, max_delay_ms=2000
)
