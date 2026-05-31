-- 身份层两层表 DDL（C2 定稿）：底层 user/conversation/message + 每平台 lark_* 表
--
-- 取代 C1 的三张 identity_* 映射表（identity_user/identity_conversation/identity_message）。
-- 注意 prod 现状：identity_* 三表仍是**旧 ULID 形态（internal_*_id 是 varchar(26)，不是 uuid）**——
-- channel-server 代码里的 entities/identity-mapping.ts 已改成 @PrimaryColumn uuid，但那份 DDL
-- 从未 apply 到 prod。所以 C2 既要落两层拆分，也要把 PK 从 ULID(varchar26) 真正换成 UUIDv7。
--
-- 本文件是 C2 的建表定义产物，**不在本任务执行**。实际 apply 是后续在 coe-* 独立泳道、
-- 由用户决策的破坏性变更（建表 + 改协议 + 回填 250 万行 + Qdrant 属 coe-*，见项目泳道选型）。
-- 执行顺序、回填脚本、回滚见 docs/plan/identity-migration-runbook.md。
-- ops-db submit 存单行、开头 -- 注释会吞整串 DDL，提交时请去掉本注释块。
--
-- =============================================================================
-- 设计依据（2026-05-30/31 ops-db 真查 prod，不是印象）
-- =============================================================================
-- information_schema 实测 identity_* 三表当前列形态：
--   internal_*_id    = character varying(26)   <- ULID，不是 uuid（C1 UUIDv7 DDL 未 apply）
--   channel          = character varying(64)   全 lark
--   channel_*_id     = character varying(256)
--   created_at       = timestamp default now()
-- 行数 / 键形态：
--   identity_user        1158 行  channel_user_id = open_id（906 ou_ + 252 on_）
--   identity_conversation 174 行  channel_conversation_id = chat_id（174 oc_）
--   identity_message    61827 行  channel_message_id = om_（飞书裸 id）
--
-- conversation_messages（实测列：含 id bigint 代理键 + username varchar(100)，C1 文档没提）：
--   总 2,503,205 行（实时写入中，数字会飘）
--   message_id      varchar(100)  om_ 2,440,851 / ULID(len26) 61,649
--   root_message_id varchar(100)  om_ 2,441,002 / ULID         61,649
--   reply_message_id varchar(100) nullable  null 2,125,952 / om_ 368,670 / ULID 8,285
--   user_id         varchar(100)  on_/ou_ 2,438,189 / ULID 61,649（仅 1127 distinct ULID 用户）
--   chat_id         varchar(100)  oc_     chat_type varchar(10)
--   create_time     bigint        13 位毫秒；实时写入（last 2026-05-30，仍在写）
--   id bigint(代理 PK 等价物)、content text、role varchar(20)、message_type varchar(30) default text、
--   bot_name varchar(50)、response_id varchar(100)、username varchar(100)、created_at timestamp
--
-- 无损性实测（关键，证实 memory 基线"孤儿 ULID=0、可无损迁移"成立）：
--   conversation_messages 里 length(message_id)=26 的 ULID 行，按
--     identity_message.internal_message_id::text = cm.message_id
--   反查：61649 行全部命中、**孤儿 0**。即 ULID 是旧内部 id（= identity_message 的 PK），
--   identity_message.channel_message_id 持对应的飞书裸 om_。所以这些 ULID 行能无损翻回 om_
--   再统一铸 uuid。（注意：identity_message 只 61827 行，远小于 244 万 om_ 历史消息——它只覆盖
--   走过 resolver 的近期/ULID 消息；244 万直接以 om_ 落库的历史消息**不在** identity_message，
--   所以铸 uuid 的全集来源必须是 conversation_messages 的 distinct 裸 id，不是 identity_message。）
--
-- 与 detail 文档 ER 唯一实证不符之处：身份键是 open_id 不是 union_id。
--   identity_user.channel_user_id 与 conversation_messages.user_id 全是 open_id（ou_/on_）。
--   detail ER 把 lark_user PK 画成 union_id 是错的。本 DDL 用 open_id 作 lark_user PK
--   （与 live resolver + 历史消息一致），union_id 降为可空属性列。open_id↔union_id+name 的桥是
--   现存 lark_user_open_id 表（app_id,open_id PK → union_id,name）。
--
-- =============================================================================
-- 命名 footgun（必须知道）
-- =============================================================================
-- `user` 是 PG 保留字（SQL 标准关键字），所有引用必须双引号 "user"。`message`/`conversation`
-- 不是保留字。本 DDL 严格按 detail 文档命名 user/conversation/message，并全程把 "user" 加引号。
-- 强烈建议执行前与用户确认是否改名 app_user/chat_user 规避双引号 footgun——是个待拍板点。

BEGIN;

-- =============================================================================
-- 底层表（平台无关，uuid PK，绝不出现裸 id；下游 agent/Qdrant/dashboard 只认它）
-- =============================================================================

-- 用户（平台无关）。channel 是判别符（枚举），不是裸 id。display_name 平台无关冗余。
CREATE TABLE "user" (
    internal_user_id  UUID         PRIMARY KEY,   -- 应用层 uuidv7() 生成（小写）
    channel           VARCHAR(64)  NOT NULL,      -- lark / qq / ...；写入即定永不变
    display_name      VARCHAR(256),               -- 平台无关显示名，可空（历史可补，对应现 username 列）
    created_at        TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 会话（平台无关）。scope 从平台 chat_type 归一：p2p->direct，group->group。
CREATE TABLE conversation (
    internal_conversation_id  UUID         PRIMARY KEY,
    channel                   VARCHAR(64)  NOT NULL,
    scope                     VARCHAR(16)  NOT NULL,  -- direct / group（归一自 chat_type）
    created_at                TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 消息（平台无关）。content 是平台无关 Content[] JSONB（detail 文档第 1 节判别联合）。
-- 现状 conversation_messages.content 是 text；迁移时按 adapter 归一成 Content[] JSONB。
-- internal_conversation_id / internal_user_id 外键回底层；internal_root_id 自指回复链可空。
CREATE TABLE message (
    internal_message_id       UUID         PRIMARY KEY,
    channel                   VARCHAR(64)  NOT NULL,
    internal_conversation_id  UUID         NOT NULL
        REFERENCES conversation(internal_conversation_id),
    internal_user_id          UUID         NOT NULL
        REFERENCES "user"(internal_user_id),
    internal_root_id          UUID
        REFERENCES message(internal_message_id),   -- root 回复链自指，根消息处为自身或空
    internal_reply_id         UUID
        REFERENCES message(internal_message_id),   -- reply_message_id 对应（现状 73% 为 null）
    role                      VARCHAR(20)  NOT NULL,    -- user / assistant
    content                   JSONB        NOT NULL DEFAULT '[]'::jsonb,  -- 平台无关 Content[]
    create_time               BIGINT       NOT NULL,    -- 平台事件毫秒时间戳（沿用现状 13 位 ms 语义）
    created_at                TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 出站反查（uuid -> channel）与按会话拉历史的常用路径。
CREATE INDEX idx_message_conversation ON message(internal_conversation_id, create_time);
CREATE INDEX idx_message_user         ON message(internal_user_id);
CREATE INDEX idx_message_root         ON message(internal_root_id);

-- =============================================================================
-- 飞书 channel 表（裸 id PK，平台专属字段 + FK 回底层 uuid）
-- 新增平台 = 新增 qq_user/qq_conversation/qq_message 三张，底层表和飞书表都不动。
-- =============================================================================

-- 飞书用户：open_id 作裸 id PK（与 live resolver + conversation_messages.user_id 一致）。
-- union_id 是跨 app 凭据，降为可空列（来源 lark_user_open_id.union_id）。name/avatar/is_admin
-- 是飞书专属 profile（来源现 lark_user(union_id PK)，经 open_id<->union_id 桥关联）。
CREATE TABLE lark_user (
    open_id           VARCHAR(256) PRIMARY KEY,   -- 飞书裸 id（per-app）
    internal_user_id  UUID         NOT NULL UNIQUE
        REFERENCES "user"(internal_user_id),
    union_id          VARCHAR(256),               -- 飞书跨 app id，可空
    name              VARCHAR(256),               -- 飞书专属 profile
    avatar_origin     TEXT,
    is_admin          BOOLEAN
);
CREATE INDEX idx_lark_user_union ON lark_user(union_id);

-- 飞书会话：chat_id 作裸 id PK。
CREATE TABLE lark_conversation (
    chat_id                   VARCHAR(256) PRIMARY KEY,   -- 飞书裸 id（oc_）
    internal_conversation_id  UUID         NOT NULL UNIQUE
        REFERENCES conversation(internal_conversation_id)
);

-- 飞书消息：om_id 作裸 id PK。ULID 历史消息经 identity_message 反查回 om_ 后并入这里
-- （孤儿 0，可无损）；ULID 本身不进 lark_message（它是旧内部 id，不是飞书裸 id）。
CREATE TABLE lark_message (
    om_id                VARCHAR(256) PRIMARY KEY,   -- 飞书裸 id（om_）
    internal_message_id  UUID         NOT NULL UNIQUE
        REFERENCES message(internal_message_id)
);

COMMIT;
