"""Phase 2 safety wiring.

Pre-check 控制面进 graph：chat pipeline ``run_pre_safety_check`` 通过
``emit_and_wait`` emit(PreSafetyRequest) → run_pre_safety →
PreSafetyVerdict（auto-emit）；verdict 由 ``emit_and_wait`` 的
notify hook 直接 set future，不需要专门的 reply-side node。

Post-check 数据面走进程内 graph：chat pipeline emit(PostSafetyRequest) →
run_post_safety → blocked 时 return Recall → Sink.mq("recall") →
channel-server recall-worker。PostSafetyRequest 本身是瞬态触发器，持久状态
在 common_agent_response。

所有节点都跑在 agent-service 主进程；post 复用 agent-service 而不是新开
safety-worker，因为单条审计的工作量小（一次 banned word + 一次 guard LLM）。
"""
from app.domain.safety import (
    PostSafetyRequest,
    PreSafetyRequest,
    Recall,
)
from app.nodes.safety import (
    run_post_safety,
    run_pre_safety,
)
from app.runtime import Sink, bind, wire

# Pre-check：单 wire — verdict 由 emit_and_wait 的 notify() 直接消费
wire(PreSafetyRequest).to(run_pre_safety)

# Post-check：transient trigger; persisted state is common_agent_response.
wire(PostSafetyRequest).to(run_post_safety)

# Recall 出 graph 给 channel-server recall-worker
wire(Recall).to(Sink.mq("recall"))

# Placement — agent-service 主进程
bind(run_pre_safety).to_app("agent-service")
bind(run_post_safety).to_app("agent-service")
