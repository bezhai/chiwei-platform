-- T5 三类身份映射表：identity_user / identity_chat / identity_message
--
-- 本项目无 migrations 目录，schema 变更走 /ops-db submit（monitor-dashboard
-- 把它记进 schema_migrations）。本文件只是 T5 的建表定义产物，**不在本任务执行**：
-- 实际 submit/apply 是后续在 coe-* 独立泳道由用户决策的破坏性变更（spec
-- "数据与部署影响" 已明确三套映射表属于破坏性变更，必须在 coe-* 做）。
--
-- 三张表结构完全相同，对应 IdentityResolver 的三个独立命名空间
-- user / chat / message。把"channel 内 ID"翻译成 channel 无关的全局内部 ID：
--   (channel, channel_*_id)  ->  internal_*_id
-- 复合唯一约束保证同一 channel 内同一外部 ID 只映射一个全局 ID。它是并发
-- 首次出现时的收敛点：应用层写路径用单 SQL upsert
--   INSERT ... ON CONFLICT ON CONSTRAINT uq_identity_*_channel DO NOTHING
-- 由 PG 引擎在单条语句内收敛（事务安全、不依赖隔离级别），不再 check-then-
-- insert-catch。**约束名 uq_identity_user/chat/message_channel 是被
-- ON CONFLICT ON CONSTRAINT 按名引用的，不可改名**（与
-- infrastructure/dal/repositories/identity-store.ts 的 forwardConstraint
-- 及 entities/identity-mapping.ts 的 @Index 名严格一致）。
-- internal_*_id 是主键，本身全局唯一；用 ULID（26 位 Crockford base32 字符串，
-- 按时间前缀单调递增、做主键索引/分区比随机 UUID 友好，128bit 熵保证跨
-- channel 不会撞）。internal_*_id 由应用层 IdentityResolver 生成后写入。
--
-- 整段在一个事务里跑，中途失败不留半形态。
-- ops-db submit 存单行、开头 -- 注释会吞整串，提交时请去掉本注释块。

BEGIN;

-- 用户身份映射：(channel, channel_user_id) -> internal_user_id
CREATE TABLE identity_user (
    internal_user_id  VARCHAR(26)  PRIMARY KEY,
    channel           VARCHAR(64)  NOT NULL,
    channel_user_id   VARCHAR(256) NOT NULL,
    created_at        TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_identity_user_channel UNIQUE (channel, channel_user_id)
);

-- 会话身份映射：(channel, channel_chat_id) -> internal_chat_id
CREATE TABLE identity_chat (
    internal_chat_id  VARCHAR(26)  PRIMARY KEY,
    channel           VARCHAR(64)  NOT NULL,
    channel_chat_id   VARCHAR(256) NOT NULL,
    created_at        TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_identity_chat_channel UNIQUE (channel, channel_chat_id)
);

-- 消息身份映射：(channel, channel_message_id) -> internal_message_id
CREATE TABLE identity_message (
    internal_message_id  VARCHAR(26)  PRIMARY KEY,
    channel              VARCHAR(64)  NOT NULL,
    channel_message_id   VARCHAR(256) NOT NULL,
    created_at           TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_identity_message_channel UNIQUE (channel, channel_message_id)
);

COMMIT;

-- 飞书历史一刀切迁移（属于 T5 后续步骤、不在本步执行，仅说明映射表怎么被填充）：
-- 对每个旧的 union_id / chat_id / message_id，以 channel='lark'、
-- channel_*_id=原飞书ID 插入映射表并分配 ULID 全局 ID，再原地重写那 7 张表
-- 加 Qdrant。回填脚本与调用方改造是 T5 接线步骤，不属于本步范围。
