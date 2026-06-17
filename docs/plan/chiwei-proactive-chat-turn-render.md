# 赤尾发给真人的消息统一走 chat-turn 人设渲染

## Problem

赤尾发给真人的消息现在两条路径口径不一:真人私聊的即时回复走 chat-turn(主模型 + 人设)说人话,但她主动发(proactive)的消息走 life 层的 offline-model(gpt-5.5)直出——干巴巴、堆游戏黑话、管真人叫"主人",很出戏。根因是 proactive 的内容由 life 工具循环里 `send_message(uid, content)` 当场把 content 写好就 emit 出站,完全没过 chat-turn 人设。想复用 chat-turn 渲染又被它对源消息 id 的强依赖挡住:proactive 是新发、没有被回复的源消息。此外,真人私聊回复完还会额外唤醒一轮 life,浪费且让她用 gpt 对真人对话重复反应。

## Goal

赤尾发给真人的所有消息——私聊回复和主动发——都由 chat-turn(主模型 + 人设)渲染,口径一致、都说人话。chat-turn 的内容渲染不再强依赖源消息 id,新发场景可用;proactive 渲染时按 chat_id 带上她跟该真人的历史对话,主动发接得上、不突兀。真人私聊回复后,life 把这件事作为被动事件感知到,但不被唤醒去单独跑一轮。

## Non-goals

- 不改飞书出站侧:channel-server 对 `is_proactive` 无 root 走 sendText 新发已经就绪。
- 不动群聊在有源消息时的 reply 链上下文能力——有源消息照常,只是把它降为可选。
- 不改 life 决定"要不要发、发给谁、想说什么"的自主决策(她管 what,这是她的自主性,不加规则)。
- 不靠调 life 的 prompt 去教 offline-model 对真人说人话——那是治标,换个离线模型又犯,且混淆职责边界。

## Key design decisions

1. **职责分离:life 管 what,chat-turn 管 how。** life 只决定"要给谁发、想说的要点/意图",真正对真人说出口的措辞交给 chat-turn 人设渲染。选这个而非在 life 层调 prompt:走 chat-turn 是治本,让主动发和私聊回复同一个口径。

2. **两套 context 构建 + 一个共享渲染层。** 真人聊天的 context 构建现在埋在 `chat_node`/`agent_stream` 流程里、还耦合源消息——把它剥离成**独立一套**;proactive 不复用这一套,而是**单独做一套 life context 构建**,输入是 life 的意图 + 按 chat_id 捞的历史,不碰源消息。两套 context 都喂给**同一个渲染层**(人设 prompt + 主模型生成),保证发给真人的消息口径统一、都说人话。选两套 context 构建而非一个参数化的渲染核心:真人(用户消息触发)和 proactive(life 意图触发)的上下文来源和语义差别大,分两套比在一个函数里 `if` 源消息有没有更清晰,也避免实施者退回伪消息补丁。proactive **不穿**真人回复专属的队列形态(`route_chat_node → chat_node` 那套的 agent_response fan-out、reply 链、pre-safety 都是回复语义,硬套到新发上只会无谓放大复杂度)。

3. **proactive 渲染带历史上下文。** 按 chat_id 捞她跟该真人的历史对话当上下文,主动发能接着上次聊。这同时是解耦的自然结果——渲染靠 chat_id 取历史,本就不需要源消息 id。

4. **真人私聊后 life 感知但不唤醒。** 真人对话作为被动事件留进她的生活(下次自然醒能读到「跟原智鸿聊过」),但不触发额外 life 轮。选这个而非完全隔离(她的生活该包含跟真人的互动)或保持现状(唤醒一轮浪费、且让她用 gpt 重复反应)。

5. **proactive 的持久化与幂等契约明确,不沾真人回复那套。** proactive 渲染**不写** agent_response(那是真人回复的 persona fan-out 和去重机制),出站契约保持现状(root_id 与 session_id 留空、worker 不反查源消息、走 sendText 新发);幂等去重用 life `send_message` 触发源派生的 durable act id,而不是派生一个聊天 session。避免渲染为复用而写 agent_response,污染 worker 记录、persona 归属和下次 history 读取。

## Caller coverage

涉及改动的现有函数与其调用方(已读代码确认):

- **chat-turn 渲染链**:`route_chat_node`(入口强校验源消息 id 非空)、`chat_node`、`build_chat_context`(拿源消息 id 反查历史和 root 链)、`agent_response` 写入(用 session_id + 源消息 id 关联)。改动:源消息 id 改为可选,无源消息时按 chat_id 取历史。调用场景:真人私聊(p2p)、群聊——两者都有源消息,解耦后行为不变;新增 proactive 无源消息场景。
- **`send_message` 工具**(life_tools,真人 LarkP2PTarget 分支):现在直接 emit 出站 segment。改动:改为产出"要点/意图",经 chat-turn 渲染后再出站。调用场景:life 主动给真人发消息。
- **真人私聊回灌**(`_replay_conversation_to_mailbox` / chat_node 回灌点):现在对 p2p 无条件回灌并唤醒 life。改动:改为被动事件(感知不唤醒)。调用场景:真人私聊回复完成后。

## Data & deployment impact

- **agent_response 表**:proactive 渲染**不写** agent_response(见决策 5),保持出站既有契约,幂等用 life act id 派生;不动表结构。
- **Langfuse**:proactive 渲染复用 chat 的 `main` 人设 prompt 还是单独 prompt,实现时定,倾向先复用 `main`(同一人设口径),不够再拆。
- **部署**:改动集中在 agent-service(渲染链 + life_tools + 回灌)。channel-server 出站侧已就绪,确认本次无需再改 channel-server;若 send_message 出站契约字段变动需同步,则一并部 channel-server 三服务。
- **后台任务**:部署会中断 coe 在跑的 world/life 推演,验证前确认无关键在跑任务。

## Tasks

1. **剥离真人聊天 context 构建 + 抽出共享渲染层**
   - 目标:把真人聊天的 context 构建从 `chat_node`/`agent_stream` 流程里剥离成独立一套、不再耦合源消息;把下游「人设 prompt + 主模型生成」抽成可被复用的渲染层。
   - 产出:真人聊天 = 独立 context 构建 → 共享渲染层;真人私聊和群聊回归不退化。
   - 验收:单测覆盖剥离后的真人 context 构建 + 渲染层;真人私聊在 coe 实测回复质量不变。

2. **life/proactive 单独一套 context 构建 + 接共享渲染层**
   - 目标:为 proactive 做一套 life context 构建(输入 = life 意图 + chat_id 历史),接 task 1 的共享渲染层出站。
   - 产出:`send_message` 真人分支不再直出文本,改为 life context 构建 → 共享渲染层(**不穿** chat 队列、**不写** agent_response、出站契约保持);life 仍只决定发不发、发给谁、说什么要点;渲染失败时不回退发 life 原文,而是不出站并把「没发出去」作为工具结果喂回 life。
   - 验收:coe 实测——赤尾主动发给真人的消息是主模型人设口径(说人话、不堆黑话、不叫「主人」),能接上历史对话;且 history 把她自己发过的(含上一条 proactive)认作她自己说的、不当成真人输入。

3. **真人私聊后 life 感知不唤醒**
   - 目标:真人私聊回复完,life 把这件事作为被动事件感知到,但不被唤醒去单独跑一轮。
   - 产出:真人私聊回灌改为被动事件——即时投递和 `renotify_unread` 补敲都不唤醒 life,但 `list_unread_events` 下次仍能读到。
   - 验收:coe 实测——真人私聊后没有额外的 life 轮被触发(从 PG/trace 看),且 life 下次自然醒时能读到该事件。

4. **coe 端到端验证**
   - 目标:三件事一起在 coe 真机验通。
   - 产出:验证证据(真人私聊回复 + 主动发的实际内容、PG/trace 佐证 life 未被多余唤醒)。
   - 验收:真人私聊回复和主动发都说人话、带历史;life 不被多余唤醒;整条链路通。
