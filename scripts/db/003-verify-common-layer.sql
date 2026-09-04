-- Read-only verification for the common channel cutover.

\set ON_ERROR_STOP on

-- common_message 的列与索引跟两侧 ORM 实体（packages/ts-shared/src/entities/
-- common-message.ts 与 apps/agent-service/app/data/models.py）取齐。这三列是建表
-- 之后加的，只活在实体里过，脚本漏了就会出现"从脚本建的库跑不起来"——ORM 会把
-- 全部列写进 INSERT，缺一列就是运行期 column does not exist。
DO $$
DECLARE
  missing text;
  outbound_indexes text;
BEGIN
  SELECT string_agg(want.column_name, ', ' ORDER BY want.column_name)
    INTO missing
  FROM (VALUES
    ('mentioned_common_user_ids', '_uuid'),
    ('agent_outbound_id', 'uuid'),
    ('recalled_at', 'timestamptz')
  ) AS want(column_name, udt_name)
  WHERE NOT EXISTS (
    SELECT 1 FROM information_schema.columns c
    WHERE c.table_schema = 'public'
      AND c.table_name = 'common_message'
      AND c.column_name = want.column_name
      AND c.udt_name = want.udt_name
      AND c.is_nullable = 'YES'
      AND c.column_default IS NULL
  );
  IF missing IS NOT NULL THEN
    RAISE EXCEPTION
      'common_message drifted from the ORM entities: % missing, wrong type, NOT NULL, or defaulted',
      missing;
  END IF;

  -- agent_outbound_id 上只能有一个索引，名字是线上已经存在的那个。两个名字并存
  -- 就是同一列上两份写放大，而且看不出它们是同一件事。
  SELECT string_agg(indexname, ', ' ORDER BY indexname)
    INTO outbound_indexes
  FROM pg_indexes
  WHERE schemaname = 'public'
    AND tablename = 'common_message'
    AND indexdef LIKE '%(agent_outbound_id)%';
  IF outbound_indexes IS DISTINCT FROM 'ix_common_message_agent_outbound_id' THEN
    RAISE EXCEPTION
      'expected exactly one index ix_common_message_agent_outbound_id on agent_outbound_id, found: %',
      COALESCE(outbound_indexes, '(none)');
  END IF;
END $$;

SELECT 'common_user' AS table_name, count(*) AS rows FROM common_user
UNION ALL SELECT 'common_conversation', count(*) FROM common_conversation
UNION ALL SELECT 'common_message', count(*) FROM common_message
UNION ALL SELECT 'common_agent_response', count(*) FROM common_agent_response
UNION ALL SELECT 'common_bot_presence', count(*) FROM common_bot_presence
UNION ALL SELECT 'lark_message', count(*) FROM lark_message;

SELECT 'unmapped_lark_user_open_id' AS check_name, count(*) AS count
FROM lark_user_open_id
WHERE common_user_id IS NULL;

SELECT 'unmapped_lark_base_chat_info' AS check_name, count(*) AS count
FROM lark_base_chat_info
WHERE common_conversation_id IS NULL;

SELECT 'common_message_missing_conversation' AS check_name, count(*) AS count
FROM common_message m
LEFT JOIN common_conversation c
  ON c.common_conversation_id = m.common_conversation_id
WHERE c.common_conversation_id IS NULL;

SELECT 'common_agent_response_bad_trigger' AS check_name, count(*) AS count
FROM common_agent_response r
LEFT JOIN common_message m
  ON m.common_message_id = r.trigger_common_message_id
WHERE m.common_message_id IS NULL;

SELECT 'common_bot_presence_missing_conversation' AS check_name, count(*) AS count
FROM common_bot_presence p
LEFT JOIN common_conversation c
  ON c.common_conversation_id = p.common_conversation_id
WHERE c.common_conversation_id IS NULL;
