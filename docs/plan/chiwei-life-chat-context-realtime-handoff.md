# 赤尾 chat→life 对话感知重构 — 开发交接文档

> 给新会话接手用。当前分支 `feat/chiwei-group-proactive`。
> 本文记录 chat→life 对话感知重构的落地状态与本轮补丁。

## 需求

把赤尾(AI 角色)"记得刚跟谁聊过"的对话感知**重新组织**:按会话(chat)结构化、
**带上她自己的回复**、放进她**每轮醒来能合理感知**的地方。核心是让她知道"自己最近
跟谁聊了什么、自己说过什么"。

## 背景:旧机制为什么不行

旧路径:chat 聊完往 life 信箱回灌一条 event(`_replay_conversation_to_mailbox`),
`summary` **只有对方一句原话、没有赤尾自己的回复** → 她醒来看到一句没回应过的话、
可能重复再回。两个死结让"在信箱里滚动更新成最新对话"走不通:

1. 信箱未读靠 `EventRead` 表反连接判定、**没有重置接口** → 同一条 event 即使 upsert
   更新了内容,读过(已写 EventRead)就永远被过滤,看不到更新。
2. 赤尾的回复是 **chat-response-worker 异步**写进 `common_message` 的、**晚于回灌那一刻**
   → 回灌时根本查不到她刚说的话。

根因:**"一个会话此刻最新的一段对话"是持续状态,不该塞进"离散未读事件信箱"这个抽象。**

## 新思路(已定)

life 醒来跑一轮时,**实时**从 `common_message` 拉她相关会话(真人私聊 + 白名单群)的
最近一段对话,渲染成「最近聊过的对话」段进她每轮 USER 输入。chat 聊完**不再回灌内容
event**;私聊和白名单群聊都改成单独 `emit(EventArrived)` 纯唤醒。这样未读重置、
回复落库时序两个坑都不存在(读取那一刻库里是什么就看到什么)。

本轮补丁额外收敛两个点：

1. 最近聊天读取不再每轮固定回看 6 小时；优先用上一轮 `LifeState.observed_at` 做增量
   水位，只有冷启 / 脏时间才回退 6 小时，避免“重复看旧聊天”。
2. 纯聊天唤醒可能没有 EventEnvelope，所以 `round_id` / `act_id` 的种子加入本轮
   `common_message_id`；否则所有空信箱聊天轮都会撞成同一轮，被旧 marker 错挡。

## 改动分解(T1 / T2 / T3 已完成落盘)

### T1 查询层 — ✅ 已完成、已落盘、已 review

- `app/data/message_record.py`(+29):新增两个 dataclass `LifeChatMessage`
  (`message_id`/`speaker_display_name`/`is_self`/`text`/`cst_time`)、`LifeChatConversation`
  (`chat_id`/`scope`(`"direct"`/`"group"`)/`display_name`/`messages`)。纯类型、无逻辑。
- `app/data/queries/messages.py`(+154,**老文件、原有 21 个方法没动**):新增
  `find_persona_related_chats_recent(*, persona_id, since_ms, max_conversations, per_chat_limit)
  -> list[LifeChatConversation]` + `_life_chat_message` helper。逻辑三步:
  ① 找她 `since_ms` 后**发过言**的会话(assistant 行 + 发言 persona 经
     `COALESCE(common_agent_response.persona_id, bot_config(bot_name→persona_id) 兜底)`);
  ② 白名单:私聊放行、群过 `should_feed_chat_to_life`(在 tx 外做,因读 Dynamic Config 是网络 IO);
  ③ 逐会话拉最近 `per_chat_limit` 条(剔 `proactive_trigger` 伪消息),折成 `LifeChatMessage`。
  - `is_self` = role=assistant 且发言 persona == 当前 persona;真人展示名用 `sender_display_name`
    兜底、不暴露 raw user_id;时间 `cst_time.to_cst_hm(str(create_time))`(parse 支持 Unix 毫秒,已确认)。
- `tests/data/test_persona_related_chats.py`(新增):**10 个 DB 集成测试**(testcontainers 真 PG),
  覆盖私聊返回/群白名单(内进外不进)/被动在场没发言不算/recency 窗口/每会话条数上限+升序/
  会话数上限/别的 persona 发言不算她/proactive 出站(bot_config 兜底)/**承重红线:真人 user 行
  带 bot_name 不误标 is_self**/proactive_trigger 剔除。

### T2 渲染层 — ✅ 已完成、已落盘、已 review

- `app/nodes/life_wake.py`(+71):新增 `_format_recent_chats(conversations)` 渲染函数 +
  3 个规模上限常量(`_RECENT_CHAT_SINCE_MS = 6h`、`_RECENT_CHAT_MAX_CONVERSATIONS = 5`、
  `_RECENT_CHAT_PER_CHAT_LIMIT = 10`)+ 在 `_run_life_round` 的 stimulus 拼接处接入
  (notebook 段之后、时间锚之前,`try` 调查询 + **fail-soft**；`since_ms` 优先取上一轮
  `LifeState.observed_at`，冷启 / 脏时间才退回 6h)。
  - 渲染:按会话分块(群标群名、群名缺失兜底「· 一个群里：」**不拼 None**;私聊「· 一段私聊里：」),
    每条「（时间）发言人：内容」,`is_self` → 「我」。**忠实呈现、不加工成叙述、不截断单条**。
  - fail-soft(查询失败 → logger.warning + 段缺席、本轮照常跑)**已被 closed-loop test 覆盖
    实证兜住**(闭环里 common_message 表不存在、查询抛错被吞、闭环不挂)。
- `tests/nodes/test_life_wake_recent_chats.py`(新增):5 个纯函数渲染测试(私聊群分组/is_self→我/
  真人persona展示名/群名缺失兜底不拼None/不截断不加工)。

### T3 — ✅ 已完成、已落盘、已 review

废旧回灌 + 群补纯唤醒 + 清 external 死代码:

- `app/nodes/chat_node.py` 已删 `_replay_conversation_to_mailbox` / `_REPLAY_MAX_CHARS` / 回灌调用。
- 已新增 `_wake_life_after_chat`:私聊直接
  `emit(EventArrived(lane=current_deployment_lane() or "prod", persona_id=...))`;群(非 p2p)
  过白名单 `should_feed_chat_to_life` 后 emit;白名单外群不唤醒;
  唤醒失败只 log 不抛,不拖垮已 emit 的 chat 回复。
- `app/domain/world_events.py` 已删 `EVENT_KIND_EXTERNAL` / `EVENT_KIND_EXTERNAL_PASSIVE`,
  `PASSIVE_EVENT_KINDS` 只保留 `EVENT_KIND_SURROUNDINGS`;mailbox/life_wake/feed_whitelist 文案
  已从旧回灌语义收口。
- 旧测试 `tests/nodes/test_chat_node_conversation_replay.py` 和
  `tests/nodes/test_chat_node_life_feed_whitelist.py` 已删;新增
  `tests/nodes/test_chat_node_group_wake.py` 覆盖私聊 emit 且不查白名单、群白名单内 emit、白名单外不 emit、
  emit 失败 fail-soft。`tests/domain/test_event_mailbox.py` 的 `test_external_passive_*` 专项已删。
- **边界(动错=回退)**:不动 surroundings(`EVENT_KIND_SURROUNDINGS` 留在 `PASSIVE_EVENT_KINDS`)、
  不动姐妹间 speech/message、不动 T1/T2。`should_feed_chat_to_life` 函数保留(T1 在用)。
- **触发机制参考**:`emit` 在 `app/runtime/emit.py`;`EventArrived` 在 `app/domain/world_events.py`
  (transient,只有 lane+persona_id);life 由 `EventArrived` → `life_wake_node`(life_wake.py:509)→
  `_run_life_round`(:614);world 由 `WorldTick` → `world_tick`(world/engine.py:814)→
  `_run_world_round`(:859)。**没有 `run_world_once`/`/admin/world/wake`**(上个会话有 Explore 编造过,不存在)。

## 已补:T2 端到端验证测试

已在 `tests/integration/test_world_life_closed_loop.py` 新增
`test_life_wake_includes_realtime_recent_chat_context`:建 common_* SQLAlchemy 表 + bot_config DDL,
seed 一段真人私聊(真人话 + 赤尾 assistant 回复),用 ambient event 触发 life 醒来,断言
`ctl.life_calls[-1]["messages_text"]` 含「最近聊过的对话」段、真人话、她回复显示「我」。

## 测试架构(新会话必读)

**两套数据模型 → 两套建表机制**:
1. **framework `Data`**(`WorldState`/`EventEnvelope`/`LifeState`/`ActPerformed`...):测试用
   `migrate(DataCls, test_db)`(`tests/runtime/conftest.py`,逐个、精确、隔离);生产/启动用
   `ensure_business_schema()`(`app/runtime/bootstrap.py`,遍历注册表全建)。底层同 `build_create_table_sql`。
2. **SQLAlchemy `Base`**(`CommonMessage`/`CommonConversation`/`CommonAgentResponse`,`app/data/models.py`):
   `Base.metadata.create_all(tables=[...])`。`scope` 取值 `"direct"`(私聊)/`"group"`(群)。
   persona 归属不是 common_message 直接列,要 join `common_agent_response`(response_id→session_id→persona_id)
   或 `bot_config`(bot_name→persona_id)兜底。
3. **外部表 `bot_config`**(channel-server 管,agent-service 无模型):手写 DDL。

**closed-loop test 范式**(`tests/integration/test_world_life_closed_loop.py`,现 14 个用例):
testcontainers 真 PG + `world_db` migrate framework Data + **只 mock `Agent.run`**(按 `cfg.prompt_id`
分流 world/life,回放脚本化工具调用 `update_world`/`notify`/`sleep` 和 `update_life_state`/`act`,
**工具的真实 DB 副作用全发生**)+ fakeredis 单飞锁。seed 用 `insert_idempotent`,触发用
`world_tick(WorldTick(...))`(notify→真 EventArrived→life_wake),断言读真实 DB
(`list_unread_events`/`find_life_state`/`list_recent_acts`/`read_world_state`)或读 `ctl.life_calls`/`ctl.world_calls`。

## 待决定点

1. **speaker 展示口径**:群里别的姐妹会显示成 persona_id(`akao`/`ayana`),不是「绫奈」这种人类名
   (她自己一律「我」)。要人类名得另查 persona 的 display name。
2. 是否要把历史 `EventEnvelope.chat_id/chat_scope/chat_name` 字段在代码注释里继续保留为兼容字段。
   当前选择:保留字段,不删 durable schema。

## 当前验证状态

本轮已补私聊纯唤醒、`observed_at` 增量水位、空信箱聊天轮幂等种子。已跑 focused 回归：

- `uv run pytest tests/nodes/test_chat_node_group_wake.py tests/nodes/test_life_wake.py tests/nodes/test_life_wake_recent_chats.py tests/data/test_persona_related_chats.py tests/integration/test_world_life_closed_loop.py -q` → 126 passed
- `uv run pytest -q` → 2369 passed, 3 skipped

## 上个会话踩过的坑(给新会话警示)

- **工具间歇抽风**:同一仓库 `git status` / `git diff` / `grep` / `ls` / `Read` **互相矛盾、重跑结果会变**;
  目录列表造假(列出不存在的文件)、grep 给错路径/假命中、Edit/Write 报成功但**不落盘**、
  **子 agent 报告假**(报"完成/N passed"实际没落盘)。可靠判断只能靠:`git diff --stat`、
  `git ls-files`、`grep -c` 数字**双查一致**、用对路径 `Read`、`pytest` 数量交叉。任何单次结果别轻信。
- **子 agent 改现有文件反复不落盘**(T3、端到端 test 都假报告;T1/T2 落盘是抽到好窗口)。
  新会话工具环境应正常——但仍建议每步 `git`/`pytest` 独立验证落盘,别信"报告成功"。
- **closed-loop test 在 `tests/integration/`**,不是 `tests/world/`(上个会话被假 grep 带到错路径耗了很久)。
