-- T4 bot_config 多 channel 化：schema 变更 + 飞书凭据迁移
--
-- 本项目无 migrations 目录，schema 变更走 /ops-db submit（monitor-dashboard
-- 把它记进 schema_migrations）。本文件只是 T4 的迁移定义产物，**不在本任务执行**：
-- 实际 submit/apply 是后续在 coe-* 独立泳道由用户决策的破坏性变更（spec
-- "数据与部署影响" 已明确这属于破坏性变更，必须在 coe-* 做）。
--
-- 整段必须在一个事务里跑（先加列 -> 回填 -> 再删旧列），中途失败不留半形态。
-- ops-db submit 存单行、开头 -- 注释会吞整串，提交时请去掉本注释块。

BEGIN;

-- 1. 加 channel 列。飞书历史记录全部归属 'lark'（默认值即覆盖回填）。
ALTER TABLE bot_config
    ADD COLUMN channel VARCHAR(20) NOT NULL DEFAULT 'lark';

-- 2. 加 credentials JSONB 列（各 channel 自己的凭据结构，框架不约束形状）。
ALTER TABLE bot_config
    ADD COLUMN credentials JSONB;

-- 3. 把现有飞书 bot 散在独立列里的五件套凭据搬进 credentials JSONB。
--    只迁 channel='lark' 的记录（此刻全部记录都是 lark）。
UPDATE bot_config
SET credentials = jsonb_build_object(
        'app_id',             app_id,
        'app_secret',         app_secret,
        'encrypt_key',        encrypt_key,
        'verification_token', verification_token,
        'robot_union_id',     robot_union_id
    )
WHERE channel = 'lark';

-- 4. 完整性校验：所有 lark 记录必须已写出五件套且**非空字符串**，否则整事务回滚。
--    运行期 larkCredentials() 拒绝空字符串（length===0 即抛错）；旧裸列若是 ''，
--    只查 IS NULL 会放过、启动才炸。这里用 NULLIF(x,'') IS NULL 把空字符串也
--    挡在迁移期，与运行期 invariant 对齐——凭据缺失/空宁可在迁移期炸。
DO $$
DECLARE
    bad_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO bad_count
    FROM bot_config
    WHERE channel = 'lark'
      AND (
          credentials IS NULL
          OR NULLIF(credentials->>'app_id', '') IS NULL
          OR NULLIF(credentials->>'app_secret', '') IS NULL
          OR NULLIF(credentials->>'encrypt_key', '') IS NULL
          OR NULLIF(credentials->>'verification_token', '') IS NULL
          OR NULLIF(credentials->>'robot_union_id', '') IS NULL
      );
    IF bad_count > 0 THEN
        RAISE EXCEPTION 'bot_config credentials backfill incomplete: % lark rows missing fields', bad_count;
    END IF;
END $$;

-- 5. 删旧的独立凭据列（不留双形态，项目硬规范禁止兼容层）。
ALTER TABLE bot_config DROP COLUMN app_id;
ALTER TABLE bot_config DROP COLUMN app_secret;
ALTER TABLE bot_config DROP COLUMN encrypt_key;
ALTER TABLE bot_config DROP COLUMN verification_token;
ALTER TABLE bot_config DROP COLUMN robot_union_id;

COMMIT;

-- 验收用：迁移后一条新 QQ bot 怎么落（凭据走 credentials JSONB，channel='qq'，
-- 加载链路按 channel 解析到 qq 占位三件套、不挂）。仅示例，不在本迁移执行：
--
-- INSERT INTO bot_config (bot_name, channel, credentials, init_type, is_active, bot_role)
-- VALUES ('qqbot_dev', 'qq',
--         '{"app_id":"<qq_app_id>","app_secret":"<qq_app_secret>","bot_secret":"<qq_bot_secret>"}'::jsonb,
--         'http', true, 'persona');
