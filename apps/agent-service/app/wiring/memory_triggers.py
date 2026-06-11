"""Wire declarations for memory trigger pipelines (afterthought).

afterthought_check doesn't need bind() — placement.DEFAULT_APP
== "agent-service" already covers it. start_debounce_consumers(
app_name="agent-service") picks it up via nodes_for_app.

Naming: this file lives next to ``memory_vectorize.py`` (Phase 0+1 message
+ fragment vectorize wires). The name ``memory.py`` is already taken by
that earlier wiring module, so the trigger wires get their own file.

drift（voice 再生成）那条 debounce wire 随 voice 子系统拆除删除。
"""

from app.domain.memory_triggers import AfterthoughtTrigger
from app.nodes.memory_pipelines import afterthought_check
from app.runtime import wire

wire(AfterthoughtTrigger).debounce(
    seconds=300,
    max_buffer=15,
    key_by=lambda e: f"afterthought:{e.chat_id}:{e.persona_id}",
).to(afterthought_check)
