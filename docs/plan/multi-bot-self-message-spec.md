# 飞书机器人发送者分类与消息去重

## 目标

同群多个 bot 接收到机器人消息时，准确识别发送者，并让同一条飞书消息在公共层只有一条记录。生活引擎正常读取工具和外部 bot 的消息，在上下文中明确标记机器人；配置为 persona 的赤尾、绫奈、千凪等按真人对待。

## 已确认事实

- 目标群 `oc_a44255e98af05f1359aeb29eeb503536` 有 ayana、chinagi、chiwei、tool 四个本系统 bot。
- 2026-09-15 只读检查的最近 24 小时内，tool 的 15 条回流按普通用户建立了错误身份，另外 42 条外部 bot 消息缺少机器人分类。
- 入站丢弃 sender_type，并固定写 role=user；出站先发消息再写 assistant 行。两路目前都忽略 common_message 主键冲突，出站未参与入站互斥。
- agent-service 已通过 role=assistant 加 bot_name/persona 识别自己的话；其他 persona 的消息正常呈现为对方说话。
- 定时任务和工具发送并不全部经过 agent 出站落库，因此不能简单丢弃所有本系统 bot 的入站事件。

## 范围

处理飞书接收消息、公共消息投影、agent 出站落库及生活引擎的发送者展示。不改变机器人消息的阅读资格、回复决策、指令及复读规则。不清理历史数据，不修改 QQ 行为，不进行生产部署。

## 关键决策

### 发送者分类

- 以发送者 union_id 查询 bot 目录，不能把事件顶层 app_id（接收应用）当成发送者。
- 普通飞书用户保持 user。
- 已配置 persona bot 使用其 canonical common_user_id、人设名和发送 bot_name，存储继续使用 assistant，保留现有自身消息识别契约；生活引擎视为真人。
- 已配置 utility bot 使用其 canonical 身份和名称，存储为 bot；未配置的飞书 bot 也存储为 bot，身份仍按渠道标识建立。
- role 是公共消息存储分类，不直接作为模型 API 的 role 传入。使用现有 varchar 列，不新增 schema。
- 上下文中的机器人标记由存储分类推导，不能根据昵称推断；人设 bot 不加机器人标记。工具和外部 bot 的未读、查询和阅读路径保持可用。

### 入站与出站去重

- 同一 om_id 的落库在同一 PostgreSQL 事务锁下串行；锁覆盖重新读取映射及两张消息表写入，不覆盖飞书 API 或资料查询。
- 保留现有按 om_id 的入站 Redis 锁，覆盖跨接收 bot 的身份准备；已配置 bot 直接复用配置身份。发送者身份目录包含停用配置，接收客户端仍只启用 active bot。
- 入站已有映射时复用 canonical common_message_id；若身份准备期间出站先落库，投影及自身根引用必须收敛到最终 ID。
- 入站先到时按正确发送者分类落库，出站随后在同一行补齐出站权威内容、身份及 agent_outbound_id / response_id，不能因忽略冲突丢失关联。
- 出站先到时，入站不能改写出站记录。回流中的 mentions 可补齐出站未记录的点名事实，不清除撤回状态。
- 出站重新读取映射必须在事务锁内，避免两路各自生成 ID 后留下孤立 common_message。
- 补写不修改已落库 event_time，避免移动游标位置、使已读消息重新成为未读。
- 去重保证以飞书真实 message_id 为边界。平台成功但缺少 message_id 时，现有合成 ID 无法与真实回流关联，此异常不宣称已解决。

## 调用方与数据影响

- websocket、webhook 和泳道交接共用解析及投影，三种入口采用同一分类。
- 入站群聊、私聊、回复链、同群跨接收 bot 均复用发送者身份和消息 ID。
- agent 出站主动发送、被动回复、分段回复共用去重行为，保留出站关联及撤回能力。
- 生活引擎消息正文及发送者摘要暴露机器人身份；自身判断仍按 persona 归属。
- 新数据采用准确分类，历史错误行不做批量回写。部署涉及 lark-service/lark-outbound 和 agent-service，验证先使用隔离环境；此次交付仅本地代码与验证。

## Tasks

1. **发送者分类**：产出统一的发送者解释及投影；验收普通用户、人设、工具、未知 bot，以及同群不同接收 bot 的身份一致性。
2. **消息去重**：产出入站和出站协调后的持久化；验收入站先到、出站先到、并发到达均只有一条完整公共记录和一条渠道映射，保留点名及撤回信息。
3. **上下文呈现**：产出生活引擎读取和展示的机器人标记；验收工具和未知 bot 可读且标记明确，人设和真人不被标为机器人，自身消息不成为未读。
4. **验证与评审**：产出针对分类及竞争顺序的回归证据和 T3 评审结论；验收相关测试和类型检查通过，明确未完成的部署验证。

## T1 评审处理

来源：codex-worker / 默认模型 / spec-review，2026-09-15。

- 采纳跨接收方身份约束：保留 Redis 入站锁，并测试每个接收方获得相同发送者身份。
- 采纳停用配置覆盖：身份目录与接收客户端启用范围分离，停用 persona 仍按人设分类。
- 采纳排序稳定性：出站补写保留现有 event_time。
- 采纳真实 PostgreSQL 验证：在临时测试数据库验证多连接竞争、事务回滚和重试。
- 采纳无真实消息 ID 的边界说明：不把合成 ID 的异常分支纳入去重保证。

## 实现与 T3 评审处理

来源：codex-worker / 默认模型 / code-review，2026-09-15；完成一轮独立静态评审，修正后的验证由主会话执行。

- 采纳 P2 同名身份误标：搜索查询直接返回完整的 `(name, is_owner, is_bot)` 发送者对象，渲染不再按名字反查可信身份。补充主人与同名机器人的回归测试。
- 采纳 P2 停用配置缺少身份：身份目录加载在事务中锁定配置行，初始化缺失 common_user_id 并确保公共用户存在；保持 is_active 原值。真实 PostgreSQL 覆盖两个启动者同时初始化停用配置。
- 采纳 P3 回复场景覆盖：补充群聊、私聊的被动分段回复，验证 response_id、根消息、回复目标和每段独立映射。
- 工具 bot 经统一出站路径发送时也保持 role=bot；无 persona 展示名时使用配置名称，避免补写时清空发送者名称。
- 数据副作用：首次启动新实现可能为停用的飞书 bot 补写公共身份及 bot_config.common_user_id，不启用其客户端。不回写历史消息。

## 验证证据

命令除注明外从仓库根目录执行，数据库测试使用本机临时 PostgreSQL 容器，端口绑定 127.0.0.1，结束自动销毁。

| 验证 | 命令 | 实际结果 |
|---|---|---|
| 原始飞书基线 | 在 apps/lark-service 执行 `bun test` | 1339 pass / 0 fail |
| 分类测试红阶段 | `bun test src/lark/message/read-message-event.test.ts src/lark/projection/inbound-projection.test.ts` | 5 个新增分类用例失败，66 pass |
| 原始代码的竞争场景 | 原始 HEAD 临时副本执行新增 PostgreSQL 核心用例 | 0 pass / 4 fail |
| 最终飞书回归 | 在 apps/lark-service 执行 `bun test` | 1348 pass / 0 fail；数据库套件在本行未启用，另行执行如下 |
| 真实 PostgreSQL | `uv run --project apps/agent-service python apps/lark-service/scripts/test-message-persistence.py` | 9 pass / 0 fail，64 assertions |
| TypeScript 类型检查 | 在 apps/lark-service 执行 `bun run typecheck` | exit 0 |
| Python 完整相关套件（T3 修正前） | 在 apps/agent-service 执行 `uv run pytest tests/data/test_bot_scoped_message_queries.py tests/living/test_phone.py tests/living/test_reading.py -q` | 228 passed |
| T3 修正后的相关回归 | 同目录执行 `uv run pytest tests/data/test_bot_scoped_message_queries.py tests/living/test_phone.py -k 'contact or name or search or bot_messages_remain or marked or owner' -q` | 42 passed / 162 deselected |
| Python 静态检查 | 同目录执行 `uv run ruff check app/data/queries/messages.py app/living/phone.py app/living/reading.py tests/data/test_bot_scoped_message_queries.py` | All checks passed |
| Diff 格式 | `git diff --check` | exit 0 |

Python 验证仅有既有 pythonjsonlogger 弃用警告；未运行飞书真实消息的泳道端到端验证，未进行生产部署。
