"""Wire declarations for memory trigger pipelines (drift / afterthought).

drift_check and afterthought_check don't need bind() — placement.DEFAULT_APP
== "agent-service" already covers them. start_debounce_consumers(
app_name="agent-service") picks them up via nodes_for_app.

Naming: this file lives next to ``memory_vectorize.py`` (Phase 0+1 message
+ fragment vectorize wires). The name ``memory.py`` is already taken by
that earlier wiring module, so the trigger wires get their own file.
"""

from app.domain.memory_triggers import AfterthoughtTrigger, DriftTrigger
from app.infra.config import settings
from app.nodes.memory_pipelines import afterthought_check, drift_check
from app.runtime import wire

wire(DriftTrigger).debounce(
    seconds=settings.identity_drift_debounce_seconds,
    max_buffer=settings.identity_drift_max_buffer,
    key_by=lambda e: f"drift:{e.chat_id}:{e.persona_id}",
).to(drift_check)

wire(AfterthoughtTrigger).debounce(
    seconds=300,
    max_buffer=15,
    key_by=lambda e: f"afterthought:{e.chat_id}:{e.persona_id}",
).to(afterthought_check)
