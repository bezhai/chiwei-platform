# 多渠道身份全局化 —— 读取侧变更说明（T5-5c，给人 review 用）

## 这次到底解决什么问题

多渠道身份全局化的核心，是把 `conversation_messages` 这张表里的
`user_id` / `chat_id` / `message_id` / `root_message_id`，从存「飞书裸 ID」
（也就是飞书的 `union_id` / `chat_id` / `message_id`）改成存一个跨渠道统一
的全局 internal ULID。这样以后接入 QQ 等其他渠道时，同一个人在不同渠道的
消息可以挂到同一个全局身份下，而不是被飞书的 ID 形状绑死。

写入端（channel-server 的入站链路 + chat-response-worker 出站回复）已经在
PR #228（也就是 5b）里改完了：`IdentityResolver` 会在 `storeMessage` 之前
把飞书裸 ID resolve 成全局 ULID，所以现在落库的确实是全局 ULID。**5c
读取侧本身没有再动写入端的 ID resolve 逻辑。**

> **重要：这份文档原先写于"5c 读取侧改完、5b 入站链路尚未重排"那个时点，
> 现已补齐到与当前工作区代码一致。** 写完 5c 之后，同一个工作区里又做了
> 两批与它逻辑耦合、必须一起 review 的改动：(1) **5b 入站链路重排**——
> 修掉 5b commit `0229a5d` 自带的入站顺序错误（原写成
> `resolve → storeMessage → runRules`、发 MQ 还藏在 runRules 内部早于
> storeMessage，与 5b 定稿要求的 `resolve → runRules → 存库 → 发 MQ` 不符）；
> (2) **`memory.ts storeMessage` fail-loud 死分支修复**。这两批连同 5c
> 读取侧改动全在同一工作区未提交。详见本文末新增的「5b 入站链路重排（与
> 5c 同批未提交，必须一起 review）」一节，以及 PR228 全局文档
> `docs/plan/multi-channel-PR228-review.md` 的 T5-5b 章节与「codex T3
> 三轮评审与整改」一节。本节以下先讲 5c 读取侧本体。

问题在于：写入端一旦写全局 ULID，**读取端原来那套靠飞书 ID 形状取数据的
逻辑就会坏**。最典型的就是「查用户名」：原来读取端是
`JOIN lark_user ON conversation_messages.user_id = lark_user.union_id`
来拿显示名的；现在 `user_id` 是全局 ULID，不再等于任何 `lark_user.union_id`，
这个 JOIN 会全部 miss，用户名要么显示成 `user_id`（fallback），要么直接空。
读取侧必须跟着改，否则线上消息列表和 agent 看到的历史里，人名会全错或全空。

这就是 5c 做的事：把所有「靠飞书 ID 形状读数据」的下游读取方，切成读
新的全局口径，**零 fallback**（不再 `COALESCE` 回旧字段兜底，因为兜底会
掩盖问题、且全局化后兜底值本身也是错的）。具体手段是给
`conversation_messages` 加一个冗余列 `username`：写入端把发送者显示名一并
落库，读取端直接读这列，不再 JOIN `lark_user`。

---

## 变更主题一：新增 `username` 冗余列（DDL 契约 + 双端实体声明）

### 为什么改

`user_id` 全局化后，已经没有任何字段能 JOIN 回 `lark_user` 取名。要么在
读取时实时反查身份映射表（多一跳、且映射表本身这次还没全局化好），要么
在写入时把显示名冗余落库、读取时直接读。这次选了后者：写入端拿名的成本
最低（入站链路本来就解析过 `LarkUser`），读取端零 JOIN 最简单可靠。

### 改成什么

`conversation_messages` 加一列 `username VARCHAR(100) NULL`。**可空**是
刻意的：历史数据迁移前这列必然为空；写入端拉不到发送者名时也留空，**不写
脏占位**（读取端按空处理）。

### 涉及文件:行号

- `apps/agent-service/app/data/models.py:143`
  —— SQLAlchemy 实体加 `username: Mapped[str | None] = mapped_column(String(100), nullable=True)`，
  紧跟在 `user_id`（第 139 行）之后，并写了为什么 nullable 的注释（140-142 行）。
- `apps/channel-server/src/infrastructure/dal/entities/conversation-message.ts:20`
  —— TypeORM 实体加 `@Column({ length: 100, nullable: true }) username?: string;`
- `packages/ts-shared/src/entities/conversation-message.ts:16`
  —— 共享包里的同一实体声明同步加 `username?: string`（两份实体定义必须
  一致，否则 channel-server 和别的 TS 服务对同一张表的认知会漂移）。

### 注意：本文档不 apply DDL

文档只描述契约。实体声明里写了这列，但**线上 `conversation_messages`
表的 `ALTER TABLE ADD COLUMN username` 这次没有执行**（见结尾边界声明）。
读取端代码读这列、写入端代码写这列，都依赖 DDL 先就绪——这是部署前必须
确认的前置条件。

---

## 变更主题二：agent-service 读取侧切 `username` 列（核心刀，零 fallback）

### 为什么改

agent-service 这边有 4 个读取路径原来都在 JOIN `lark_user.union_id` 取名，
全局化后全部会 miss。

### 改成什么 + 涉及文件:行号

文件统一是 `apps/agent-service/app/data/queries/messages.py`。

1. **`find_username(user_id)`（123-138 行）**：原来是
   `select(LarkUser.name).where(LarkUser.union_id == user_id)`，改成查该
   全局 user 最近一条有 username 的消息行：

   ```python
   select(ConversationMessage.username)
       .where(ConversationMessage.user_id == user_id)
       .where(ConversationMessage.username.is_not(None))
       .order_by(ConversationMessage.create_time.desc())
       .limit(1)
   ```

   无 `lark_user` JOIN、无 `COALESCE` fallback。语义从「按 union_id 查
   lark_user 名」变成「按全局 user_id 查该 user 最近非空 username」——
   函数签名 `(user_id: str) -> str | None` 没变，调用方无感。

2. **`find_context_messages_for_anchors`（199-235 行）**：原来返回
   `list[tuple[ConversationMessage, LarkUser]]`，靠
   `.join(LarkUser, ConversationMessage.user_id == LarkUser.union_id)`。
   改成 `select(ConversationMessage, ConversationMessage.username)`，
   返回类型变成 `list[tuple[ConversationMessage, str | None]]`（235 行
   `[(row[0], row[1]) for row in result.all()]`）。**这是一个返回结构
   变更**，唯一消费方 history.py 已同步改（见变更主题三）。

3. **`find_messages_with_user_chat_persona_by_root`（346 行起，关键改动
   在 366 行）**：quick-search 根链查询，原 `LarkUser.name.label("username")`
   + `.outerjoin(LarkUser, ConversationMessage.user_id == LarkUser.union_id)`
   改成 `ConversationMessage.username.label("username")` 并删掉那个
   `outerjoin`。返回 tuple 形状 `(message, username, chat_name, persona_id)`
   不变，只是 username 来源换了。

4. **`find_messages_with_user_chat_persona_in_chat`（384 行起，关键改动
   在 409 行）**：与上一个 quick-search query 同构的改动，同样删
   `LarkUser` outerjoin、改读 `ConversationMessage.username` 列。

> 第 3、4 个 quick-search query 是「查用户名刀」的同类漏网项，本轮一并补
> 修——它们和 `find_username` / `find_context_messages_for_anchors` 是
> 同一个反模式（JOIN `lark_user.union_id` 取名），漏了就会在 quick-search
> 路径上人名全空。

### 怎么验证的

- 新增 `apps/agent-service/tests/unit/data/test_messages_username.py`（137 行）
  - `test_find_username_reads_conversation_messages_username_column`：钉死
    `find_username` 读的是 `conversation_messages.username`、不走 lark_user。
  - `test_find_username_returns_none_when_no_row`：无行返回 None（零 fallback）。
  - `test_find_context_messages_returns_message_and_username_string`：钉死
    新返回结构是 `(ConversationMessage, str | None)`。
- 新增 `apps/agent-service/tests/unit/data/test_messages_quick_search_global_id.py`（124 行）
  - `test_by_root_reads_username_column_no_lark_user_join`
  - `test_in_chat_reads_username_column_no_lark_user_join`
    —— 分别钉死两个 quick-search query 读 username 列、不再 JOIN lark_user。
- 全程用 mock，未连真实 DB（见结尾边界）。

---

## 变更主题三：history.py 渲染侧按行级 / role 取名（codex T3 评审后修订）

### 为什么改

这是 codex T3 评审拍出来的问题，比单纯切 JOIN 更隐蔽：

- `check_chat_history` 原来对每条非 assistant 消息调
  `find_username(msg.user_id)` 取名。`find_username` 改成「按全局 user_id
  查该 user **最近一条** 非空 username」之后，如果同一个全局 user 在不同
  渠道用过不同显示名，这里渲染一段历史会把这条老消息的说话人显示成该
  user 最新的名字——**跨消息 / 跨渠道串名**。正确做法是读这条消息行
  **自己**的 username 冗余列（行级本意 = 发这条消息当时的发送者名）。
- `search_group_history` 原来渲染 `f"...{user.name}: {content}"`，依赖
  `find_context_messages_for_anchors` 返回的 `LarkUser`。现在返回的是
  `username | None`，且 **assistant 行的 username 列本来就是空的**（只有
  user 行落发送者名）。如果直接 `username or '?'`，assistant 行会全显
  `'?'`。

### 改成什么 + 涉及文件:行号

文件 `apps/agent-service/app/agent/tools/history.py`：

- `check_chat_history`（98-104 行）：assistant 行仍显示 `"我"`（99 行）；
  非 assistant 行从 `name = await find_username(msg.user_id)` 改成
  `speaker = msg.username or "?"`（104 行，直读本行冗余列）。对应地，
  顶部 import 去掉了 `find_username`（现在只 import `find_messages_in_range`）。
- `search_group_history`（215-229 行）：解包从 `for msg, user in rows`
  改成 `for msg, username in rows`（215 行）；渲染从 `user.name` 改成按
  role 派生 speaker——`msg.role == "assistant"` 显示 `"我"`（225-226 行），
  否则 `username or "?"`（227-228 行）。assistant 行显示风格与本文件
  `check_chat_history` 保持一致（都是「我」），不再显示 botName。

### 怎么验证的

- `apps/agent-service/tests/unit/agent/tools/test_history.py` 改动
  覆盖：`check_chat_history` 读行级 username 不串名、`search_group_history`
  assistant 行显示「我」/ user 行读 username 列。
- codex 第二轮必改 3（`_context_messages` 群上下文 assistant 行渲染成
  「未知用户」）已 TDD 修：`app/chat/_context_messages.py` 新增
  `_speaker_of(msg)` 按 role 派生（assistant 返回「我」、user 行
  `username or "未知用户"`），新增
  `apps/agent-service/tests/unit/chat/test_context_messages_speaker.py`
  钉死，不再把赤尾历史发言渲染成占位词喂模型。
- agent-service 全量相关单测本会话实跑全绿（含「查用户名刀」回归 + 新增
  quick-search / Qdrant filter / proactive / `_context_messages` 说话人
  契约测试，TDD 有 red→green）。

---

## 变更主题四：channel-server / monitor-dashboard 写入 + 读取对齐

### 为什么改

读取端要读 `username` 列，前提是写入端把它写进去；monitor-dashboard 是
另一个独立读取方，原来也 JOIN `lark_user`，全局化后同样会坏。

### 改成什么 + 涉及文件:行号

写入侧（这次只是给已有 `storeMessage` 调用补 `username` 字段，没有改
5b 的 ID resolve 链路）：

- `apps/channel-server/src/types/chat.ts:20`：`ChatMessage` 接口加可选
  `username?: string`（带注释说明来源未知时为空）。
- `apps/channel-server/src/infrastructure/integrations/memory.ts:36`：
  `storeMessage` 的 INSERT values 加 `username: message.username ?? undefined`
  （没有就落 null，不抛、不写脏占位）。**同一文件还有一处与 5b 入站重排
  配套的 fail-loud 死分支修复**：原 `storeMessage` 整段包在一个 catch 里、
  DB 出错只 `console.error` 后返回 void；5b 入站重排在 `handlers.ts` 新增
  了"storeMessage 失败 → fail-loud（不 savePending / 不 publish）"的
  try/catch，但 `storeMessage` 内部把真实 PG 故障吞成 void、正常返回，
  handlers 那段 fail-loud 对真实 DB 故障就成了**永远进不去的死分支**。
  修法是删掉 `storeMessage` 函数体外层那个吞错 try/catch（`memory.ts:26`
  起函数体不再包 try），让真实故障自然上抛。去重不受影响——`.orIgnore()`
  的 `ON CONFLICT DO NOTHING` 由 PG 在 SQL 层吃掉冲突、`execute()` 正常
  返回（identifiers 为空、行已存在），根本不走任何错误路径。详见本文末
  「5b 入站链路重排」一节。
- `apps/channel-server/src/infrastructure/integrations/lark/events/handlers.ts:214`：
  入站 user 消息的 `storeMessage` 调用加 `username: message.senderInfo?.name`。
  来源是 `message.senderInfo`（`MessageBuilder.buildMetadataFromEvent` 已
  按 union_id 拉过的 `LarkUser` 行），**不新造数据源**；拉不到留空。
- `apps/channel-server/src/workers/chat-response-worker.ts:348`：assistant
  回复落库时 `username: botName || context.getBotName() || undefined`。
  assistant 行的显示名读取端按 role 派生（dashboard 显示 `赤尾`、history
  显示 `我`），这里冗余落 bot 名只是为了让列尽量非空一致，用的是现成已
  解析值，不新造数据源。

读取侧（monitor-dashboard，`apps/monitor-dashboard/src/routes/messages.ts`）：

- `/api/messages` 主查询（48 行）：原
  `CASE WHEN msg.role = 'assistant' THEN '赤尾' ELSE COALESCE(lu.name, msg.user_id) END`
  改成 `CASE WHEN msg.role = 'assistant' THEN '赤尾' ELSE msg.username END`，
  并删掉 `.leftJoin('lark_user', 'lu', 'msg.user_id = lu.union_id')`。
  assistant 行显示名仍按 role 派生为 `赤尾`，user 行直读 username 列、
  无 fallback。
- p2p 会话取名子查询：抽成模块级常量 `P2P_NAME_SQL`（11-18 行），原来
  `LEFT JOIN lark_user lu` + `COALESCE(lu.name, cm.user_id)` 改成直读
  `cm.username`，**并加了 `AND cm.username IS NOT NULL` 过滤**。这条
  过滤是关键：查询用 `DISTINCT ON (cm.chat_id) ... ORDER BY cm.chat_id,
  cm.create_time DESC` 取每个会话最新一条 user 消息的名字；如果最新那行
  username 恰好为空（写入端拉不到名时会留空），DISTINCT ON 会选中这个
  空行，把本来更早一条可用的名字丢掉。加 `IS NOT NULL` 让它跳过空行、
  落到最近一条有名字的消息上。调用处（102 行）改成引用 `P2P_NAME_SQL`。

### 怎么验证的

- 新增 `apps/channel-server/src/infrastructure/integrations/memory.username.test.ts`（89 行）：
  username 透传进 INSERT；没有时落 null（不抛、不写脏占位）。
- 新增 `apps/channel-server/src/infrastructure/integrations/lark/events/handlers.username.test.ts`（179 行）：
  入站 user 消息 `storeMessage.username = message.senderInfo?.name`；
  `senderInfo` 缺失时 `username=undefined`（无 fallback）。
- 新增 `apps/monitor-dashboard/src/routes/messages.p2pname.test.ts`（19 行）：
  钉死 p2p 取名子查询带 `cm.username IS NOT NULL` 过滤。
- channel-server 全套件本会话实跑 **201 pass / 3 fail**（这个数已含 5c
  读取侧 + 5b 入站重排 + fail-loud 死分支修复三批共同新增的全部测试）；
  `bunx tsc --noEmit` 零新增错误。3 个失败全是 `bot-var.test.ts`、属 T4
  bot_config 基线既存、本批零新增（见未决项 3）。

---

## 确认「不用改」的地方（这是经过判断的，不是遗漏）

以下读取代码看过确认无需改，仅补契约测试钉死「ID 当不透明字符串透传、
不对 ID 形状做假设」这个性质，防止以后回归：

- **`app/life/proactive.py`**：`_resolve_target_message`（54 行起）里有
  `target_message_id.isdigit()` 分支（65 行）。看过：这个分支的本意是
  「DB 自增 row id vs message_id」二选一，**不是**「飞书裸 ID vs 全局
  ULID」的判断。全局 ULID 不是纯数字，走的是 `find_message_by_id`
  那条（71 行），跟原行为一致；跨会话拒绝靠 `msg.chat_id == chat_id`
  字符串相等（73 行），ID 形状变了也不影响。**无需改**。
  - 测试：`apps/agent-service/tests/unit/life/test_proactive.py`（+119 行），
    含 `test_submit_proactive_global_ulid_target_skips_row_id_branch`、
    `test_submit_proactive_cross_chat_rejected_by_global_chat_id` 等，
    钉死全局 ULID 下行为不变。
- **`app/agent/tools/history.py` 的 Qdrant chat_id filter**（157-170 行）：
  `Filter(FieldCondition(... MatchValue ...))` 拿 `chat_id` 当不透明字符串
  做等值匹配，没有任何飞书 ID 形状假设；写入端 chat_id 全局化后，只要
  写读用同一个全局 chat_id 就自洽。**无需改**，仅补契约测试。
- **`app/data/models.py`**：除了加 `username` 列，`message_id` /
  `user_id` / `chat_id` / `root_message_id` 都是 `String(100)`，本来就
  是不透明字符串，全局 ULID 照样存得下，列定义无需动。

---

## 契约已知例外：proactive 合成行（已决策，零代码改动）

「`conversation_messages` 全字段全局 internal ULID」这条契约有**一个
显式例外，已经和你确认过、是已决定的设计，不是遗漏也不是 bug**：赤尾
主动消息（proactive）那条合成行。

`app/life/proactive.py` 的 `insert_proactive_message`（134-135 行）确实
会往 `conversation_messages` 写一行，但这一行不是真实收发的消息，是
赤尾主动发起对话时为了让这次发言进历史而合成的占位行。它的两个身份
字段刻意不是全局 ULID：

- `message_id` 是 `proactive_<ts>` 这种自造串（proactive.py:104），不是
  全局 ULID。原因是这条消息飞书侧根本不存在——是赤尾主动说的，没有对应
  的飞书 message 事件，也就没有 ID 可以走 IdentityResolver resolve。
- `user_id` 是 `__proactive__` 这个哨兵常量（`PROACTIVE_USER_ID`，
  proactive.py:20），不是某个真实用户的全局 ULID。因为这行不是任何人
  发的，是系统代赤尾合成的。

**为什么这不破坏契约、也不是 bug**：

- 这行的 `reply_message_id` / `root_message_id` 不是自造的，是从库里
  已经落好的那条 target 消息上取的（`reply_message_id = target_msg.message_id`、
  `root_message_id = target_msg.root_message_id`）。5c 把写入端切成全局
  ULID 之后，target 消息本身落的就是全局 ULID，所以 proactive 行的
  reply / root 指针**仍然是全局 ULID、和被回复的那条消息指针一致、回复
  链不断**。例外只落在这行自己的 `message_id` / `user_id` 两个字段上，
  不外溢到指针。
- 这行的 `message_id` 只会作为某条回复链的链尾出现，不会成为链路里的
  断点——没有任何后续消息会去 reply 这条 `proactive_<ts>`（飞书侧没有
  这条消息，没人能回复它）。
- 现有读取方不是「碰巧没踩坑」，是**显式靠哨兵 + message_type 识别并
  兼容它**：`get_unseen_messages` 用 `exclude_user_id=PROACTIVE_USER_ID`
  把这行排除，`get_recent_proactive_records` 反过来用
  `proactive_user_id=PROACTIVE_USER_ID` 专门捞这行。读取侧已经把这个
  哨兵当成一类已知数据来处理，不会被它的非 ULID 形状误导。

**给新增读取方的硬约束**：今后任何新写的、会读 `conversation_messages`
的逻辑，如果对 `message_id` / `user_id` 一律按全局 ULID 形状做假设
（比如拿去 join 身份映射表、或按 ULID 长度 / 字符集校验），必须先意识到
proactive 合成行是这个假设的例外，按 `PROACTIVE_USER_ID` 哨兵 +
`message_type` 把它识别出来单独处理。这条例外靠文档显式记下来，就是因为
它唯一的真实风险是认知层面的——代码上没问题，但不知道这个哨兵的人可能
误读。

---

## 调用方全覆盖核实

- **`find_context_messages_for_anchors`（返回结构变了）**：全仓 grep 确认
  唯一消费方是 `app/agent/tools/history.py` 的 `search_group_history`，
  已同步改对（变更主题三）。没有其他调用方会被这个返回结构变更打到。
- **`find_username`（语义变了、签名没变）**：范围外还有两处调用——
  `_timeline.py` 和 `app/nodes/memory_pipelines.py`。看过：它们的语义
  本来就是「按全局 user_id 查该 user 最近的名字」，这正是新 `find_username`
  的行为，签名 `(user_id: str) -> str | None` 没变，**不受影响，本次
  刻意不动**（它们要的就是「该 user 最近名」，不是「这条消息行的名」，
  跟 history.py 的 bug 是两回事）。
- **`find_group_members`**：走 `LarkGroupMember.union_id` 群花名册，是
  一条合法的独立路径（群成员名册不是按消息行取名），跟「查用户名刀」
  不是同一个反模式，**本次不动**。

---

## 未决项 / 待你决策（醒目）

### 1. `reply_message_id` 已全局化完成（原"待你拍板"，已随 5b 入站重排一并修复）

原状：写入端 `handlers.ts` 把 `reply_message_id` 写库时用的还是飞书裸
`parentMessageId`，没有 resolve 成全局 message_id，与全局 ULID 主键
形状不一致。这一项原先标为"最需要你拍板（这次补否 / 单独立项）"。

**已修复**：随同批的 5b 入站链路重排一起补掉了——`inbound-pipeline.ts`
的 `runInboundContractChain` 新增 `globalReplyToId`
（`inbound-pipeline.ts:56-59`、`119-136`），把飞书"回复某条消息"锚点
`parentMessageId` 与 root 一样经 `IdentityResolver.resolve` 翻成全局
internal_message_id；无 parent 时 `undefined`（保持原"空就空"语义、
不凭空造 id）；`handlers.ts` 的 `storeMessage` 改用
`reply_message_id: chain.globalReplyToId`（`handlers.ts:230`）。**不再
是未决项**，详见本文末「5b 入站链路重排」一节。

### 2. `/api/users` 仍返回 `lark_user.union_id AS user_id`

`user_id` 全局化后，`/api/users` 这个接口里
`lark_user.union_id AS user_id` 的过滤会失效（拿 union_id 当 user_id
对不上全局口径）。这跟「`lark_user` / `lark_group_member` 表本身全局化
后怎么处置」是同一个 spec 尚未定义的 gap，**本次范围外**，列出来让你
知道它存在。

### 3. 3 个 `bot-var.test.ts` 失败是基线既存，非本批引入

channel-server 套件里的 3 个 fail 是 pristine 基线就有的（属 T4
bot_config 改动范围），根因是 `bun mock.module` 的进程级污染（mock
跨测试文件泄漏）。**不是本批引入的**，已记为待办，本次不修。

### 4. 入站 `senderInfo.name` 非空 + 出站 PG 故障路径，需 coe 真机验证

`handlers.ts` 写入 `message.senderInfo?.name`，但「入站事件里
`senderInfo.name` 实际是否非空」靠读代码证明不了，必须 coe 真机跑一遍
看实际落库的 username。这一项连同「映射表 DDL 尚未 apply」「整体真机
一致性」「本次入站重排后 agent-service `find_message_content` 不再读空
走"未找到记录"短路」一起并入后续 coe（5e）验证。

**另一个必须诚实披露的点**：fail-loud 死分支修复后，出站
chat-response-worker 的 PG 故障路径**没有集成测试**——这条路径"落库
失败仍安全"靠的是代码结构论证（worker 自身 try/catch 未改 + 飞书消息
在落库前已发出，落库失败不影响用户已收到的回复），不是测试证明。
review 时请清楚这一处没有自动化兜底，coe（5e）需专项验证此路径。

### 5. 去重锁非原子、publish 失败无补偿（既有隐患，单独 backlog，本批不扩大）

5b 入站重排后，`handlers.ts` 抢到去重锁（`setNx`，60s TTL）才
`savePending` + `publish`；若 publish 失败，60s 内同一 message_id 的
其它 bot 因锁还在而静默跳过，这条消息既没发出去也没人补发。codex 第三
轮拍出。**裁决为既有隐患、非本次重排引入**：git 证据显示重排前
（`makeTextReply` 内 setNx 紧接 publish）与重排后（`handlers.ts` 内
setNx 紧接 publish）setNx ↔ publish 相对时序一致，这个窗口重排前就在、
本次没扩大。**单独记 backlog，不在本批夹带扩大范围。**

---

## 5b 入站链路重排（与 5c 同批未提交，必须一起 review）

这一节是这份文档补写时新增的。它讲的不是 5c 读取侧本体，而是和 5c
同一个工作区、同一批未提交的另一处改动——**修掉 5b commit `0229a5d`
自带的入站链路顺序错误**。放在这份读取侧文档里呼应，是因为它和 5c
共用 `storeMessage`、还顺手把 5c 文档原未决项 1（reply_message_id）
补掉了，owner review 5c 时绕不开它。完整设计在 PR228 全局文档
`docs/plan/multi-channel-PR228-review.md` 的 T5-5b 章节，这里只讲
和读取侧 / 数据契约相关的点。

**问题。** 5b commit 把入站接线写成了 `resolve → storeMessage →
runRules`，且"发 ChatTrigger 到 MQ"藏在 `runRules` 内部的 persona
handler `makeTextReply` 里（早于 storeMessage 完成）。5b 定稿要求的
钉死顺序是 `resolve → runRules → 存库 → 发 MQ`。后果直接打到读取侧的
对端：下游 agent-service `chat_node.py` 的
`find_message_content(message_id)` 强依赖"这条消息已落库"，MQ 比
storeMessage 先到时它读空、直接 emit "未找到相关消息记录"短路。git
证据确认这是 5b 既有缺陷、非 5c 或本批新引入，**经与用户确认本 PR
内修复**。

**改了什么（只列与数据契约 / 读取侧相关的）。**

- 发 MQ 从 `runRules` 内部抽出、推迟到 `storeMessage` 成功之后。
  `makeTextReply`（`reply.ts:83-128`）在 runRules 阶段只登记"待发
  意图"（通过 handler 新增可选第二参 `ctx.registerPendingChatTrigger`），
  不实际 publish、不取去重锁、不落 `agent_responses` pending 行；
  `engine.ts` 把意图折进唯一终态 `RuleTerminalState.pendingChatTrigger`
  （per-handler 作用域捕获，并发安全）；`handlers.ts` 顺序硬钉成
  `resolve → runRules → storeMessage(无条件) → 抢去重锁 → 拿到锁才
  savePending + publish`（`handlers.ts:201-276`）。
- `storeMessage` 无条件执行、不看 terminal kind——**非 @bot 群消息
  复读照常入库，飞书逐场景零变化**（codex 第三轮曾质疑这会被短路，
  用真实契约链证据驳回：非 @bot 群消息契约链返回 `ok:true /
  respond:false`、不短路，新增 `inbound-pipeline.real-lark.test.ts` /
  `handlers.inbound-order.test.ts` 钉死）。这一点和读取侧直接相关：
  群历史 / quick-search 读到的非 @bot 群消息行就靠这条无条件入库才在。
- `reply_message_id` 全局化（直接修掉本文原未决项 1）：
  `inbound-pipeline.ts` 新增 `globalReplyToId`（`inbound-pipeline.ts:56-59`、
  `119-136`），把飞书 `parentMessageId` 与 root 一样 resolve 成全局
  internal_message_id；`handlers.ts:230` 改用
  `reply_message_id: chain.globalReplyToId`。读取侧（`cross_chat.py` /
  `_context_messages.py` 按回复链 walk）从此不会因 `reply_message_id`
  存裸 ID 而与全局 message_id 主键失配断链。
- `memory.ts storeMessage` fail-loud 死分支修复（见变更主题四已展开）：
  删内部吞错 try/catch，让真实 PG 故障自然上抛，使 `handlers.ts` 新增
  的 fail-loud（store 失败 → 不 savePending / 不 publish）真正可达。

**验证。** 全程 TDD（红→绿）。channel-server 全套件本会话实跑
**201 pass / 3 fail**（3 fail 是 T4 `bot-var.test.ts` 基线既存、本批
零新增），`bunx tsc --noEmit` 零新增错误。新增多组真链路测试：
`engine.pending-trigger.test.ts` / `engine.pending-scope.test.ts` /
`reply.pending-trigger.test.ts` / `handlers.inbound-order.test.ts` /
`handlers.multibot-pending.test.ts` / `handlers.store-semantic.test.ts` /
`memory.failloud.test.ts` / `inbound-pipeline.real-lark.test.ts`。
**真机端到端未验证（也做不了——5b 仍是不可部署中间态，映射表未
apply、契约链必然失败）**，重排后真机验证统一并入 5e（含 agent-service
`find_message_content` 不再读空那条）。fail-loud 死分支删除这一处**未
过 codex 第四轮**（codex 调用超时、零输出），如实标注：前三轮已充分
覆盖入站链路与重排，这个删除属低风险（`storeMessage` 仅 2 个真实调用
方、语义影响已论证），但它没有 codex 外部视角背书，review 时请重点看。

---

## 本次边界声明

- **没有 commit**：以上全部是工作区未提交改动（5c 读取侧 + 5b 入站
  重排 + fail-loud 死分支修复三批同在一个未提交工作区）。
- **没有 apply DDL**：`conversation_messages.username` 列的 `ALTER TABLE`
  没有执行；实体声明只是契约，部署前必须先确认 DDL 就绪。
- **5c 读取侧没碰写入端 5b 的 ID resolve 逻辑**；但同批的 5b 入站重排
  确实改了写入端接线（`handlers.ts` / `reply.ts` / `engine.ts` /
  `inbound-pipeline.ts` / `memory.ts`），那是修 5b 既有缺陷、已与用户
  确认本 PR 内做，见上一节。
- **没动 `/api/users`**：见未决项 2，spec 未定义的 gap，范围外。
- **验证全程未连真实 DB / Qdrant**：用 mock 做单测；真机一致性（含
  入站重排端到端、出站 PG 故障路径专项）留给 coe（5e）。
- **没改 spec**：`docs/plan/multi-channel-support.md` 未改。本文档与
  `docs/plan/multi-channel-PR228-review.md` 这两份 review 说明本次同步
  补到与当前工作区代码一致。
