"""Wiring: 撤回出 graph。

  Recall -> Sink.mq("recall")

实际投的是 ``recall_{channel}``（sink dispatch 按 payload 的 ``channel`` 现算
routing key），飞书那条由 lark-service 消费。emit 它的是 ``app.living.takeback``
—— 她把已经说出去的话收回来。

原先这个模块还挂着 pre / post 安全检查那两条边（``PreSafetyRequest`` ->
``run_pre_safety``、``PostSafetyRequest`` -> ``run_post_safety``）。它们唯一的
emitter 在旧 chat pipeline 里，旧实现删掉后这两条边没有生产者了，跟着一起清掉。
她自己开口那条链**在发出去之前**就判同一件事，直接调
``app.capabilities.output_safety.audit_output``（``app.living.mouth``），不经过
graph。
"""
from app.domain.safety import Recall
from app.runtime import Sink, wire

wire(Recall).to(Sink.mq("recall"))
