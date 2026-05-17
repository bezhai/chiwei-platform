# 把飞书专属入口抽象为平台无关 channel 框架，QQ 官方机器人首落地

## Problem
整条消息链路写死飞书：事件处理器硬绑飞书事件名，回复链路深耦合飞书 SDK，**身份体系不止 user**——union_id 贯穿 7 张表（5 张是主键），chat_id/message_id/root_message_id 被当全局裸 ID 还渗进 Qdrant 向量库，群聊「是否在叫 bot」靠 robot_union_id→飞书 mention 反查。无任何「渠道」抽象，加一个平台要再改一遍核心链路。

## Goal
- lark-proxy/lark-server 飞书专属层被重写为平台无关 channel 抽象（入站事件 / 内部消息模型 / 出站回复 / bot 命中 四层契约），飞书是其中一个 adapter，飞书 dev bot 端到端对话行为不变。
- 用户、会话(chat)、消息(message) 三类身份全部平台作用域化：存在 `(platform, platform_*_id)→全局 internal id` 映射，conversation_messages、lark_* 等 7 张表 + Qdrant 已一刀切迁移，所有调用方改完，零 fallback。
- 平台无关的「消息是否触发 bot」契约成立，QQ 官方机器人 dev bot 在私聊和群聊下纯文本收发端到端打通。
- bot_config 多平台化：platform 列 + 凭据 JSONB，飞书凭据已迁入。

## Non-goals
- QQ 图片/富文本/表情/分段流式（这期只纯文本）。
- 个人 QQ（OneBot/NapCat）；微信/Discord 实际接入（框架留口不实现）。
- agent-service AI 链路（vectorize/recall/memory/safety）逻辑变更——已确认不消费身份语义，仅随契约透传。

## Key design decisions
1. **抽象重写 lark-* 为 channel-*，非并行链路**：重构规范禁兼容层，两套并行链路永远清不掉飞书耦合。爆炸半径中等（文件名/Dockerfile/Makefile SIBLINGS/默认URL/PaaS app/ConfigBundle），可控。
2. **三类身份全部平台作用域化（不止 user）**：选 (platform, platform_*_id)→全局 id 映射覆盖 user/chat/message over 只迁 user。理由：补查发现 chat_id/message_id/root_message_id 在 SQL 与 Qdrant 都当全局裸 ID，QQ 群 ID 与飞书 chat_id 是独立命名空间，只迁 user 会击穿会话维度与回复链。
3. **一刀切停机迁移，回滚=快照级 restore（非映射反查）**：选停机原地重写 over 灰度双写（违反禁兼容层）。映射表只能翻译值、不能恢复已重写的主键/外键/索引与 Qdrant 向量，故回滚定义为「迁移前 DB+Qdrant 快照 + restore」，明确是 restore 不是 rollback。停写期需 drain gate。
4. **bot 命中契约平台化（从 non-goal 升为核心）**：补查发现群聊靠 NeedRobotMention→robot_union_id→飞书 mention 反查，不满足**静默丢弃无日志**。QQ 群聊纯文本要端到端必须有平台无关的 addressed-to-bot 契约 + 平台无关 bot identity，否则 QQ 群消息被规则链静默吞掉。
5. **channel 切四层契约**：入站事件 adapter / 平台无关内部消息模型 / 出站回复 adapter / bot 命中契约。耦合恰好收敛在这四处，agent-service 中间链路对来源无知。

## Caller coverage（grep/Explore 确认，非记忆）

**含飞书身份字段的全部 7 张表**（一刀切迁移 + 快照范围）：

| 表 | 身份字段 | 约束 |
|---|---|---|
| lark_user | union_id | 主键 |
| lark_group_member | chat_id, union_id | 复合主键 |
| lark_user_open_id | app_id, open_id, union_id | 复合主键+外键 |
| user_blacklist | union_id, blocked_by | 主键+字段 |
| user_group_binding | user_union_id, chat_id | 普通字段 |
| conversation_messages | user_id(=union_id), chat_id, message_id, root_id | 字段/message_id 主键 |
| bot_config | robot_union_id | bot 身份字段 |

**身份/会话语义调用方**（需改）：agent-service `app/data/queries/messages.py`（user_id 过滤/JOIN lark_user/群成员 JOIN/主动消息写入/root_message_id 跨会话查 line 362,210）、`app/chat/quick_search.py:84`（按 chat_id 拉会话）、`app/agent/tools/history.py:163`（Qdrant 按 chat_id 过滤）、`app/life/proactive.py:73`（chat_id 定位 proactive 目标）、`app/data/models.py:138`（message_id 主键）、lark-server `chat-response-worker.ts`（写 user_id）、`bot-chat-presence.ts`（chat_id 主键）、monitor-dashboard `messages.ts:35,91`（JOIN union_id）、契约 `chat_dataflow.py` user_id（加 platform）。

**bot 命中链路**（需平台化）：lark-server `core/rules/engine.ts:29-82`（规则引擎，不过静默 break）、`core/rules/rule.ts:84`（NeedRobotMention=hasMention(botUnionId)||isP2P）、`core/models/message.ts:139`（hasMention 查飞书 mention）、`mention-utils.ts:6`（union_id 提取）、`bot-var.ts:21`（robot_union_id）。

**不受影响**：vectorize/recall/memory/safety/chat_node（仅透传，不消费身份语义）；lane_routing（route_key=bot_name）；RabbitMQ 拓扑；LaneRouter/lite-registry（已走环境变量）。

## Data & deployment impact
- **Schema 变更（`/ops-db submit`，破坏性，走 coe-* 独立泳道）**：新建 user/chat/message 三套 `(platform,platform_id)→全局 id` 映射表；7 张表身份字段+主键体系一刀切重写；Qdrant 向量库 chat_id/root_id 同步重写；bot_config 加 platform 列+credentials JSONB、飞书凭据迁入。
- **Drain gate（停写窗口）**：迁移前须 webhook 停入口 + RabbitMQ/outbox 队列清空或处置 + 一镜像多服务（channel-server/recall-worker/chat-response-worker、agent-service/vectorize-worker）同步切换后再恢复写入，否则无 fallback 的新旧契约在队列重放时互相污染。部署前确认无 in-flight rebuild/afterthought。
- **回滚**：迁移前 DB + Qdrant 全量快照；失败走快照 restore（非代码 rollback）。
- **PaaS app 改名**：lark-{proxy,server}→channel-{proxy,server} 需 PaaS 注册新 app + 迁 ConfigBundle、旧 app 下线，高风险，上线前单独跟用户确认。
- 无 Langfuse prompt 变更。QQ dev bot 需先在 QQ 开放平台建测试机器人并配 HTTPS 回调。

## Tasks
依赖：T1 是 T2/T4/T5/T6 前置；T4 是 T6 前置；T3 独立可并行；T5 在 T2 后做以免回归噪声。

### T1. channel 四层契约 + 平台作用域身份模型（地基）
- **Goal**：定义并落地平台无关的入站事件 / 内部消息模型 / 出站回复 / bot 命中 四层契约；内部模型与 ChatTrigger/ChatResponseSegment 带 platform；user/chat/message 三类身份的平台作用域抽象成型。
- **Deliverable**：channel 四层接口 + 带 platform 的内部消息/契约模型 + 三类身份映射的领域模型（不含数据迁移本身）。
- **Verification**：契约层单测覆盖「同 platform_chat_id 跨平台不串会话」「bot 命中契约在群聊/私聊两路返回正确」；agent-service 中间链路无需感知具体平台即可编译通过。

### T2. 飞书 adapter（行为不变回归）
- **Goal**：把现有飞书入站/出站/bot 命中逻辑收敛为 T1 契约下的第一个 adapter，飞书行为零变化。
- **Deliverable**：飞书 adapter（事件解析、回复发送、mention→bot 命中），飞书专有命名只存在于 adapter 内。
- **Verification**：飞书 dev bot 绑 coe 泳道，私聊+群聊+@bot+不@bot 各场景与改造前一致（实际收发日志/截图为证）。

### T3. lark-*→channel-* 部署改名（独立面）
- **Goal**：目录/包名/Dockerfile/Makefile SIBLINGS/服务间默认 URL/PaaS app/ConfigBundle 全部迁到 channel-*。
- **Deliverable**：改名后可正常构建部署的 channel-proxy/channel-server。
- **Verification**：业务代码无旧服务名调用入口；PaaS/ConfigBundle 指向 channel-*；coe 泳道部署+飞书回归通过；飞书 adapter 内部飞书专有命名保留不误改。

### T4. bot_config 多平台化
- **Goal**：bot_config 加 platform 列 + credentials JSONB，飞书凭据迁入、旧独立列清除，bot 加载按 platform 分发。
- **Deliverable**：bot_config schema 变更 + 凭据迁移 + 多平台加载链路。
- **Verification**：coe 泳道飞书 bot 仍正常加载收发；飞书凭据已在 JSONB、旧列已删；新增 platform=qq 记录可被加载链路识别。

### T5. 三类身份一刀切迁移 + 快照回滚 + drain gate
- **Goal**：建 user/chat/message 三套映射表，停机原地重写 7 张表 + Qdrant，飞书侧 (lark,*) 入映射，改完 Caller coverage 全部「需改」调用方，零 fallback。
- **Deliverable**：三套映射表 + drain gate 迁移过程 + DB/Qdrant 快照与 restore 预案 + 调用方改造。
- **Verification**：coe 泳道迁移后飞书用户/会话历史查询结果与迁移前一致；新写入为全局 id；Qdrant 按新 id 检索正确；快照 restore 演练成功；代码无双读/fallback 路径。

### T6. QQ 官方机器人 adapter（私聊+群聊纯文本）
- **Goal**：实现 QQ adapter——webhook 接入 + Ed25519 验签 + 回调校验 + 事件转内部模型 + AccessToken 鉴权自刷新 + 平台无关 bot 命中适配 + 纯文本回复，覆盖私聊与群聊。
- **Deliverable**：QQ 入站+出站+bot 命中 adapter，接 T1 契约，凭据走 T4 JSONB。
- **Verification**：QQ 开放平台测试机器人绑 coe 泳道，私聊与群聊各发纯文本、群聊@bot 与不@各一条，赤尾按 bot 命中契约正确响应/不响应，端到端日志为证。
