"""Phase 2 safety wiring.

Pre-check 控制面进 graph：chat pipeline emit(PreSafetyRequest) → run_pre_safety
→ PreSafetyVerdict → resolve_pre_safety_waiter（把 verdict 塞回本进程 Future）。

Post-check 数据面走 durable：chat pipeline emit(PostSafetyRequest) → durable
queue → run_post_safety → blocked 时 return Recall → Sink.mq("recall") →
lark-server recall-worker。

所有节点都跑在 agent-service 主进程；post 复用 agent-service 而不是新开
safety-worker，因为单条审计的工作量小（一次 banned word + 一次 guard LLM）。
"""
from app.domain.safety import (
    PostSafetyRequest,
    PreSafetyRequest,
    PreSafetyVerdict,
    Recall,
)
from app.nodes.safety import (
    resolve_pre_safety_waiter,
    run_post_safety,
    run_pre_safety,
)
from app.runtime import Sink, bind, wire

# Pre-check：双段 in-process wire
wire(PreSafetyRequest).to(run_pre_safety)
wire(PreSafetyVerdict).to(resolve_pre_safety_waiter)

# Post-check：durable
wire(PostSafetyRequest).to(run_post_safety).durable()

# Recall 出 graph 给 lark-server recall-worker
wire(Recall).to(Sink.mq("recall"))

# Placement — 4 个节点都在 agent-service 主进程
bind(run_pre_safety).to_app("agent-service")
bind(resolve_pre_safety_waiter).to_app("agent-service")
bind(run_post_safety).to_app("agent-service")
