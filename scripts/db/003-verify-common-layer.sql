-- Read-only verification for the common channel cutover.

\set ON_ERROR_STOP on

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
