# 身份层迁移 Runbook（C2，已废弃）

> **废弃说明**：本 runbook 是旧 identity_* 方案草案，不再作为当前迁移依据。
> 当前实现以 `common_*` 作为平台无关层，Lark 映射保留在 `lark_*`，执行入口是
> `scripts/db/run-common-layer-migration.sh` 与
> `scripts/db/002-backfill-common-layer.mjs`。历史 UUIDv7 只能由 JS 脚本调用
> `uuid` 包的 `v7()` 生成，禁止手写 UUIDv7 算法或在 SQL 中自造生成函数。

> 把混合态身份层（旧 ULID + 飞书裸 id）迁到 UUIDv7 两层表（底层 `"user"`/`conversation`/`message`
> + 每平台 `lark_*`）。配套 DDL：`channel-layer-identity-mapping-tables.sql`；数据模型：
> `channel-layer-redesign-detail.md`。
>
> **本文件是草案，不在本任务执行。** 真执行要等用户拍板下面的「执行前提」，并在 coe-* 独立泳道、
> 带 pg_dump + Qdrant snapshot 演练后才上 prod。所有 SQL 是草案，执行前在 coe 上跑通验证查询再定稿。

---

## 0. 实证现状（2026-05-30/31 ops-db 真查 prod，不是印象）

| 对象 | 实证 |
|---|---|
| identity_user | 1158 行，channel 全 lark，channel_user_id = **open_id**（906 ou_ + 252 on_） |
| identity_conversation | 174 行，channel 全 lark，channel_conversation_id = chat_id（174 oc_） |
| identity_message | 61827 行，channel 全 lark，channel_message_id = om_（飞书裸 id） |
| identity_* 的 PK 形态 | `internal_*_id` 是 **varchar(26)=ULID，不是 uuid**——C1 的 UUIDv7 DDL 从未 apply 到 prod |
| conversation_messages | 2,503,205 行（实时写入中，数字会飘），有 `id bigint` 代理列 + `username` 列 |
| ├ message_id varchar(100) | om_ 2,440,851 / ULID(len26) 61,649 |
| ├ root_message_id | om_ 2,441,002 / ULID 61,649 |
| ├ reply_message_id（nullable） | null 2,125,952 / om_ 368,670 / ULID 8,285 |
| └ user_id varchar(100) | on_/ou_(open_id) 2,438,189 / ULID 61,649（仅 1127 distinct ULID 用户） |
| create_time | 13 位毫秒；**仍在实时写**（last write 2026-05-30，非冻结表） |
| Qdrant point id | agent-service `app/nodes/_ids.py` 用 `uuid5(固定NS, message_id)` 确定性派生——**point id 是 message_id 字符串的 uuid5 哈希**，换 message_id 字符串就换 point id |

**与 memory 基线对账**：iu/ic/im 三表行数、混合比例（~244 万 om / ~6.2 万 ULID）、孤儿=0
**全对得上**。基线唯一不够精确处：identity_* 在 prod 仍是 ULID(varchar26) 形态，UUIDv7 只在代码里、
未上 prod；且 conversation_messages 仍在实时写（不是冻结历史表）。

**无损性实证（证实 memory "孤儿 ULID=0、可无损迁移"成立）**：conversation_messages 里
length(message_id)=26 的 61649 行 ULID，按 `identity_message.internal_message_id::text = cm.message_id`
反查全部命中、**孤儿 0**。即 ULID 是旧内部 id（= identity_message 的 PK），
`identity_message.channel_message_id` 持对应飞书裸 om_。所以 ULID 行能无损翻回 om_ 再统一铸 uuid。
**但 identity_message 只 61827 行 << 244 万 om_ 历史**——它只覆盖走过 resolver 的近期/ULID 消息，
244 万直接以 om_ 落库的历史不在表内，所以铸 uuid 的全集来源是 conversation_messages 的 distinct
裸 id，不是 identity_message。

---

## 执行前提（用户必须先拍板，未拍板不得执行）

- **P1 — lark_user PK 用 open_id**：确认（本 DDL 采用，与 live resolver + 历史消息一致）。
  union_id profile（name/avatar）经现存 lark_user_open_id 桥关联。涉及 lark_group_member /
  user_blacklist / user_group_binding 仍按 union_id 键，是否一并切 uuid 或保留 union_id 域，需定。
- **P2 — conversation_messages 实时写入**：实测 last write 2026-05-30，**它仍在被实时写**。所以
  drain gate（步骤 ⑤）必须真关死入口，不能当冻结表跳过。迁移窗口期入站消息要么停、要么排队。
- **P3 — UUIDv7 生成（已改）**：PG 无原生 uuidv7()；当前迁移脚本只能通过
  `uuid` 包的 `v7()` 生成，禁用手写算法和 `gen_random_uuid()`。
- **P4 — 表名 `"user"`**：`user` 是 PG 保留字需全程双引号。是否改名 app_user/chat_user 规避，需定。
- **P5 — Qdrant 回填策略**：point id = uuid5(NS, message_id)。message_id 从 ULID/om_ 改成 uuid 后，
  同一条消息的 point id 会变。两条路：① 改 _ids.py 让 uuid5 输入用 internal_message_id，重算全量
  point id 重建两 collection；② 直接用 internal_message_id 当 point id（它本就是 uuid，省一层 uuid5）。
  两 collection（messages_recall / messages_cluster）的实际 point 数量 + payload 里是否另存裸 id，
  ops-db 查不到，**须 C2 执行时用 Qdrant 客户端核实后再定脚本**。

---

## 迁移分五步，顺序不能错（混合态）

每步格式：动作 SQL/命令草案 → 验证查询 → 回滚动作。回滚按**逆序**（⑤→①）。

### 步骤 ① canonical 化：ULID 行归一回飞书裸 om_

**目标**：把 conversation_messages 的 ULID 旧内部 id 翻回飞书裸 om_，让身份列只剩裸 id
（om_/oc_/open_id），后续按裸 id 统一铸 uuid。无损依据：步骤 0 实测孤儿 0。

动作草案（直接用现存 identity_message 做反查表，不需外部 dump）：
```sql
BEGIN;
-- message_id：ULID -> om_（identity_message.internal_message_id 是 ULID，channel_message_id 是 om_）
UPDATE conversation_messages cm
   SET message_id = im.channel_message_id
  FROM identity_message im
 WHERE im.internal_message_id::text = cm.message_id
   AND length(cm.message_id) = 26;
-- root_message_id / reply_message_id 同样翻
UPDATE conversation_messages cm
   SET root_message_id = im.channel_message_id
  FROM identity_message im
 WHERE im.internal_message_id::text = cm.root_message_id
   AND length(cm.root_message_id) = 26;
UPDATE conversation_messages cm
   SET reply_message_id = im.channel_message_id
  FROM identity_message im
 WHERE im.internal_message_id::text = cm.reply_message_id
   AND length(cm.reply_message_id) = 26;
-- user_id：ULID -> open_id（identity_user 同结构，channel_user_id 是 open_id）
UPDATE conversation_messages cm
   SET user_id = iu.channel_user_id
  FROM identity_user iu
 WHERE iu.internal_user_id::text = cm.user_id
   AND length(cm.user_id) = 26;
COMMIT;
```
验证查询（应全 0）：
```sql
SELECT count(*) FILTER (WHERE length(message_id)=26)      AS ulid_msg_left,
       count(*) FILTER (WHERE length(root_message_id)=26) AS ulid_root_left,
       count(*) FILTER (WHERE length(reply_message_id)=26) AS ulid_reply_left,
       count(*) FILTER (WHERE length(user_id)=26)          AS ulid_uid_left
FROM conversation_messages;
```
回滚：本步在事务内，失败 ROLLBACK；已 COMMIT 后靠步骤 ⑤ 的 pg_dump restore。

### 步骤 ② 按新 schema 建两层表，扫全集铸 UUIDv7 映射

**目标**：建底层 `"user"`/`conversation`/`message` + `lark_*`（DDL 文件），扫归一后**全部 distinct
裸 id** 铸 uuidv7 填映射。**铸 id 全集来源是 conversation_messages，不是 identity_message**
（后者只覆盖 61827 条）。

动作草案：
```sql
-- 2a 建表：执行 channel-layer-identity-mapping-tables.sql（coe 上先跑通）
-- 2b lark_message + 底层 message：一次性脚本读 SELECT DISTINCT message_id（并入 root/reply 引用到的
--    所有 om_）-> uuid.v7() 生成 uuid -> COPY 进 lark_message(om_id, internal_message_id)
--    与 message(internal_message_id, ...)。禁用手写算法和 gen_random_uuid（v4 无序）。
-- 2c lark_user + 底层 "user"：SELECT DISTINCT user_id(open_id) -> uuidv7；
--    union_id/name/avatar 经 lark_user_open_id（open_id->union_id->lark_user profile）关联回填。
-- 2d lark_conversation + 底层 conversation：SELECT DISTINCT chat_id -> uuidv7；
--    scope 由 conversation_messages.chat_type 归一（p2p->direct，group->group）。
-- 2e 底层 message 回填外键：JOIN lark_* 把 conversation_messages 每行的裸
--    message/root/reply/user/chat 翻成对应 uuid 写入 message 行。
```
验证查询：
```sql
-- 裸 id 全集都进 channel 表、无遗漏
SELECT (SELECT count(DISTINCT message_id) FROM conversation_messages) AS d_msg,
       (SELECT count(*) FROM lark_message) AS lark_msg;        -- 应相等
SELECT (SELECT count(DISTINCT user_id) FROM conversation_messages) AS d_uid,
       (SELECT count(*) FROM lark_user) AS lark_user;          -- 应相等
SELECT (SELECT count(DISTINCT chat_id) FROM conversation_messages) AS d_chat,
       (SELECT count(*) FROM lark_conversation) AS lark_conv;  -- 应相等
-- 底层 message 外键无悬空
SELECT count(*) FROM message m
  WHERE NOT EXISTS (SELECT 1 FROM conversation c WHERE c.internal_conversation_id=m.internal_conversation_id)
     OR NOT EXISTS (SELECT 1 FROM "user" u WHERE u.internal_user_id=m.internal_user_id);
-- root/reply 自指可解析
SELECT count(*) FROM message m WHERE m.internal_root_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM message r WHERE r.internal_message_id=m.internal_root_id);
```
回滚：`DROP TABLE lark_message, lark_conversation, lark_user, message, conversation, "user" CASCADE;`
（新表是增量，drop 不动 conversation_messages 原表）。

### 步骤 ③ 回填 7 表 + Qdrant 2 collection，裸 id -> uuid

**目标**：把所有**读/写身份 id 的旧载体**从裸 id 切到底层 uuid。复用步骤 ② 铸的映射。

涉及表（影响清单见末节，逐个回填，改前各建 `_bak`）：
```sql
-- conversation_messages：message_id/root/reply -> internal_message_id；user_id -> internal_user_id；
--   chat_id -> internal_conversation_id（username 列可保留为冗余显示名）
-- agent_responses：trigger_message_id/chat_id -> uuid；session_id 关联看是否绑 message
-- bot_chat_presence：chat_id -> internal_conversation_id
-- user_group_binding：user_union_id/chat_id（union_id 须先经 lark_user_open_id 桥转 open_id 再转 uuid，P1）
-- user_blacklist：union_id（同上经桥，P1）
-- lark_group_member：chat_id/union_id（同上经桥，P1）
-- lark_group_chat_info / lark_base_chat_info：chat_id -> internal_conversation_id
```
Qdrant（**P5 须先核实**）：
```
# messages_recall / messages_cluster：按 P5 选定策略重算 point id（uuid5 输入改 internal_message_id，
# 或直接以 internal_message_id 当 point id），scroll 全量 -> 算新 id -> upsert 重建 -> count 校验一致
# -> 切别名 -> 删旧 collection。payload 里若另存裸 message/chat/user id，一并改 uuid。
```
验证查询：
```sql
SELECT count(*) FILTER (WHERE message_id !~ '^[0-9a-f-]{36}$') AS non_uuid_msg,
       count(*) FILTER (WHERE user_id  !~ '^[0-9a-f-]{36}$') AS non_uuid_uid,
       count(*) FILTER (WHERE chat_id  !~ '^[0-9a-f-]{36}$') AS non_uuid_chat
FROM conversation_messages;   -- 全 0 才算切净
-- Qdrant：抽样断言 point id/payload id 全 uuid，count(new)=count(old)
```
回滚：本步**就地改原表**，风险最高。每张表改前 `CREATE TABLE x_bak AS SELECT * FROM x`，失败
`TRUNCATE x; INSERT INTO x SELECT * FROM x_bak;`。Qdrant 靠 ⑤ 的 snapshot restore。`_bak` 在 ④ 验收前不删。

### 步骤 ④ 下游切全局 uuid，零 fallback

**目标**：channel-server 出站反查走 `lark_*`（uuid->裸 id）；agent-service 及下游只收 uuid、
不碰裸 id、不反查（边界铁律）。**零兼容**：切完删旧的双 id 读路径，不留 fallback。

代码改造（属各服务分支，本 runbook 只列验收口径）：
- channel-server：IdentityStore/DbIdentityResolver 从 identity_* 三表切到新两层表
  （现按约束名 `uq_identity_*_channel` ON CONFLICT DO UPDATE RETURNING 收敛 forward-key，
  新表 upsert 路径要同构重写：lark_* 裸 id 唯一约束兜并发 + 底层 insert 出 uuidv7）。
- agent-service：recall / vectorize / safety / cluster 全链路 payload 只见 uuid；
  `_ids.py` 按 P5 调整 point id 派生。
验证：coe 上 dev bot 端到端发一条 -> 全链路 trace 身份字段全 uuid，飞书侧仍正确收到回复
（证明出站反查对）。**禁止"应该没问题"**，要 trace/日志为证。
回滚：下游代码 revert 到读裸 id 上一版 + 旧表未删时可读。所以步骤 ③ 的 `_bak` 在 ④ 验收前不删。

### 步骤 ⑤ drain gate + 快照 + coe 演练 + 删旧

**目标**：真关死入口旧写路径（P2：conversation_messages 实时写，不能当冻结表），全量快照兜底，
coe 全程演练通过后才上 prod，最后删旧。

顺序（这步是上 prod 的总闸）：
1. **drain**：停 channel-server 入站消费、等队列（safety/vectorize/recall）drain 到 0。
   验证：`SELECT count(*) FROM conversation_messages WHERE create_time > <drain 起点 ms>;` 应恒 0。
2. **快照**：`pg_dump`（业务库全量）+ Qdrant snapshot（两 collection）。记录位置/时间戳。
3. **coe 演练**：①~④ 全套在 coe-<name> 跑一遍（schema 已建 + 种子数据复刻到 chiwei-test），
   验证查询全绿 + dev bot 端到端通过。
4. **上 prod**：维护窗口按 ①->④ 执行，每步跑验证查询，任一不绿立即停。
5. **删旧（零兼容收尾，验收通过后）**：`DROP TABLE identity_user, identity_conversation,
   identity_message;` + 删步骤 ③ 所有 `_bak` 表 + 删旧 Qdrant collection。
回滚（总回滚）：任一步炸 -> 停 -> `pg_restore` 到步骤 2 快照 + Qdrant snapshot restore + 下游代码 revert。
**回滚靠快照 restore，不靠逆向 SQL**（混合态逆向不可靠）。

---

## 影响清单：所有读/写身份 id 的表与服务

> 「迁移后行为」=该载体身份列从裸 id 变成底层 uuid；「需改读取方」=是否有代码读它、必须同步切。

### channel-server（身份解析唯一入口；entities 在 src/infrastructure/dal/entities/）

| 表 | 身份列 | 现状 | 迁移后 | 需改读取方 |
|---|---|---|---|---|
| conversation_messages | message_id/root/reply/user_id/chat_id | 裸 id（om_/open_id/oc_）+ ULID 残留 | 全 uuid | 是：所有读历史/拼 context 的代码 |
| agent_responses | trigger_message_id/chat_id/session_id | 裸 id | uuid | 是 |
| bot_chat_presence | chat_id（PK 之一） | 裸 id（oc_） | internal_conversation_id | 是 |
| lark_base_chat_info / lark_group_chat_info | chat_id（PK） | 裸 id | 成为 lark_conversation profile，chat_id 留作裸 id 加 uuid 关联 | 是：群信息读取 |
| lark_group_member | chat_id + union_id（复合 PK） | 裸 id（union_id） | 经桥转 uuid 或保留 union_id 域（P1） | 是 |
| user_group_binding | user_union_id + chat_id | union_id + chat_id | 经桥转 uuid（P1） | 是 |
| user_blacklist | union_id（PK） | union_id | 经桥转 uuid（P1） | 是：黑名单校验 |
| lark_user | union_id（PK）, name/avatar | 旧 profile 表 | 新 lark_user 改 open_id PK + union_id 列；旧表 profile 经桥并入 | 是 |
| lark_user_open_id | app_id+open_id（PK）, union_id, name | open_id↔union_id 桥 | **迁移的桥，保留**（步骤 ②/③ 靠它转 union_id↔open_id） | 否（迁移工具用） |
| identity_user/conversation/message | internal_*_id(ULID), channel, channel_*_id | C1 三表（prod 仍 ULID） | 被两层表取代，步骤 ⑤ DROP | 是：IdentityStore/DbIdentityResolver 切新表 |
| bot_config | persona_id, bot_name | 非身份 id（bot/persona），不迁 | 不变 | 否 |

代码核心：`infrastructure/dal/repositories/identity-store.ts`、`core/channels/db-identity-resolver.ts`、
`core/channels/identity-resolver*.ts`、`infrastructure/integrations/identity-resolver-runtime.ts`
是身份解析核心，步骤 ④ 必须改成读写新两层表。

### agent-service 及下游（边界铁律：只见全局 uuid、不碰裸 id、不反查）

身份 id 触达的源码（grep message_id/chat_id/user_id 命中，非穷举）：`app/data/models.py`、
`app/data/queries/messages.py`、`app/domain/message.py` / `message_request.py`、
`app/chat/_context_messages.py` / `context.py`、`app/agent/tools/history.py` / `recall.py`、
`app/nodes/vectorize.py` / `_ids.py`、`app/memory/recall_engine.py` / `vectorize_memory.py` /
`conflict.py` / `cross_chat.py`、`app/capabilities/vector_store.py`、`app/infra/qdrant.py`。
注意 agent-service 与 channel-server **共用同一业务库**（conversation_messages / lark_* 同物理表）；
点 id 派生在 `app/nodes/_ids.py` 的 `vector_id_for(message_id) = uuid5(NAMESPACE_DNS, message_id)`。

| 对象 | 身份载体 | 迁移后行为 | 需改读取方 |
|---|---|---|---|
| Qdrant messages_recall | point id = uuid5(NS, message_id) + payload | point id 随 message_id 变 uuid 而重算（P5） | 是（_ids.py + 重建 collection） |
| Qdrant messages_cluster | 同上 | 同上 | 是（P5） |
| recall / vectorize / safety 队列消息体 | message/user/chat 身份字段 | 由 channel-server 注入即 uuid，下游透传 | 否（上游切净即可；coe 须验无裸 id 漏入） |
| agent-service 自有 chat_id 表 | glimpse_state.chat_id / fragment.chat_id（+ memory_entity/abstract_memory/memory_edge/notes/reply_style_log 经 memory 链带 chat_id） | 裸 chat_id（oc_） | internal_conversation_id | 是：记忆/glimpse/life 全链路 |
| agent-service 镜像的 lark_* SQLAlchemy 模型 | lark_user(union_id PK)/lark_group_member(chat_id+union_id)/lark_group_chat_info/lark_base_chat_info | 与 channel-server 同表（共库），SQLAlchemy 镜像 | 随 channel-server 侧切；agent-service ORM 模型同步改 | 是：messages.py 的 find_group_members JOIN union_id |
| agent-service conversation_messages 读取 | message_id/root/reply/user_id/chat_id（messages.py 全文用） | 裸 id | uuid | 是：cross_chat/quick_search/context/history/glimpse 全部读 |

> agent-service 与 channel-server **共用同一业务库**（conversation_messages / lark_* 是同物理表，
> agent-service 用 SQLAlchemy 镜像、channel-server 用 TypeORM）。所以步骤 ③ 改这些表一次即对两服务生效，
> 但**两套 ORM 模型 + 所有读 SQL 都要同步改**（agent-service `app/data/models.py` 的 ConversationMessage
> message_id/user_id/chat_id 等列语义、`app/data/queries/messages.py` 全部查询、`find_group_members`
> 的 `LarkGroupMember.union_id == LarkUser.union_id` JOIN、`resolve_message_id_by_row_id` 现假定
> message_id 是 om_）。C2 执行时仍须 ops-db 列 agent-service 独有库表（akao_schedule/life_engine_state/
> schedule_revision/model_* 等）的 information_schema，确认是否另存身份 id 漏网。

---

## 最大风险点（一句话）

最难回滚的是步骤 ③ 就地改 250 万行原表 + 重建两个 Qdrant collection，只能靠 ⑤ 的
pg_dump/snapshot 兜底（`_bak` 与快照在全程验收通过前一律不准删）；其次 prod identity_* 仍是
ULID 形态、conversation_messages 仍在实时写（drain 必须真关死，不能当冻结表）；身份键是
open_id 不是 union_id（颠覆 detail 文档 ER，本 DDL 已据实修正、union_id 经 lark_user_open_id 桥关联）。
