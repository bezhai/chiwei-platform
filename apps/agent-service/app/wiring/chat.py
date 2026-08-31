"""Phase 5a chat 主 pipeline wiring。

  Source.mq("chat_request")
       ─[wire ChatTrigger, in-process]─→  route_chat_node
                                              │
                                              ↓ N × emit(ChatRequest)
       ─[wire ChatRequest, .durable()]─→  chat_node
                                              │
                                              ↓ N × emit(ChatResponseSegment)
       ─[wire ChatResponseSegment, in-process]─→  Sink.mq("chat_response")
                                              ↓
                       chat_response_{channel} → 拥有该 channel 的渠道服务
                       （飞书 → lark-service，QQ → channel-server）

ChatTrigger 用 transient=True，幂等去重在 ChatRequest 上由 (message_id,
persona_id) 联合 Key 完成；ChatRequest 持久化所以走 ``.durable()``，
ChatResponseSegment transient=True 又是 sink 出 graph，不需要 durable。
所有 @node 跑在 agent-service 主进程（默认 app），因此不需要 bind。

**入站那两条边在 living 实验泳道上不注册**（``app.living.experiment``）。不加这道门，
dev bot 发一条消息，旧 chat 和新引擎会各回一份人；而且新引擎的整个设计前提是"chat 是
嘴，没有耳朵"——旧 chat 的 ``chat_request`` 消费者活着，这个前提当场就不成立了。
消息不会丢：它照样落 ``common_message``，新引擎每一缝直接查库（``app.living.phone``）。

**出站那条边不关。** ``ChatResponseSegment -> Sink.mq(chat_response)`` 是新旧两套共用
的唯一出口，新引擎说话就是走它出去的。跟入站一起关掉，她就成了哑巴而不是"没有耳朵"。
"""
from app.domain.chat_dataflow import ChatRequest, ChatResponseSegment, ChatTrigger
from app.domain.chat_events import CommonMessageContentSynced
from app.living.experiment import on_the_living_experiment_lane
from app.nodes.chat_node import chat_node, route_chat_node
from app.nodes.persist_tos_files import persist_tos_files_node
from app.runtime import Sink, Source, wire

# 出站：两套引擎共用，任何泳道都在。
wire(ChatResponseSegment).to(Sink.mq("chat_response"))

if not on_the_living_experiment_lane():
    wire(ChatTrigger).from_(Source.mq("chat_request")).to(route_chat_node)
    wire(ChatRequest).to(chat_node).durable()

    # Phase 6 v4 Gap 5: build_chat_context emits CommonMessageContentSynced
    # instead of fire-and-forget asyncio.create_task. Durable so the DB write
    # runs out of band of the chat stream while still landing in the
    # agent-service main process (matching the old asyncio.create_task
    # placement). 只可能由 chat_node 的上下文构建触发，所以跟着入站一起走：
    # 没有 chat_node 的泳道上它是一条死边，留着只白占一条 durable 队列。
    wire(CommonMessageContentSynced).to(persist_tos_files_node).durable()
