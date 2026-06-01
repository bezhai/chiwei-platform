-- Agent-service common-layer cutover helpers.
-- This script is idempotent. It creates common_bot_presence and rewrites
-- agent-service runtime/state chat/message ids from Lark raw ids to common ids.

CREATE TABLE IF NOT EXISTS common_bot_presence (
  common_conversation_id uuid NOT NULL REFERENCES common_conversation(common_conversation_id),
  bot_name varchar(50) NOT NULL,
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(common_conversation_id, bot_name)
);

CREATE INDEX IF NOT EXISTS idx_common_bot_presence_bot
  ON common_bot_presence(bot_name);

INSERT INTO common_bot_presence (
  common_conversation_id,
  bot_name,
  is_active,
  created_at,
  updated_at
)
SELECT
  lb.common_conversation_id,
  bp.bot_name,
  bool_or(bp.is_active) AS is_active,
  min(bp.created_at) AS created_at,
  max(bp.updated_at) AS updated_at
FROM bot_chat_presence bp
JOIN lark_base_chat_info lb
  ON lb.chat_id = bp.chat_id
WHERE lb.common_conversation_id IS NOT NULL
GROUP BY lb.common_conversation_id, bp.bot_name
ON CONFLICT (common_conversation_id, bot_name) DO UPDATE SET
  is_active = EXCLUDED.is_active,
  updated_at = EXCLUDED.updated_at;

UPDATE data_chat_request d
SET chat_id = lb.common_conversation_id::text
FROM lark_base_chat_info lb
WHERE d.chat_id = lb.chat_id
  AND d.chat_id LIKE 'oc_%'
  AND lb.common_conversation_id IS NOT NULL;

UPDATE data_chat_request d
SET message_id = lm.common_message_id::text
FROM lark_message lm
WHERE d.message_id = lm.om_id
  AND d.message_id LIKE 'om_%';

UPDATE data_chat_request d
SET root_id = lm.common_message_id::text
FROM lark_message lm
WHERE d.root_id = lm.om_id
  AND d.root_id LIKE 'om_%';

UPDATE data_glimpse_request d
SET chat_id = lb.common_conversation_id::text
FROM lark_base_chat_info lb
WHERE d.chat_id = lb.chat_id
  AND d.chat_id LIKE 'oc_%'
  AND lb.common_conversation_id IS NOT NULL;

UPDATE glimpse_state g
SET chat_id = lb.common_conversation_id::text
FROM lark_base_chat_info lb
WHERE g.chat_id = lb.chat_id
  AND g.chat_id LIKE 'oc_%'
  AND lb.common_conversation_id IS NOT NULL;

UPDATE fragment f
SET chat_id = lb.common_conversation_id::text
FROM lark_base_chat_info lb
WHERE f.chat_id = lb.chat_id
  AND f.chat_id LIKE 'oc_%'
  AND lb.common_conversation_id IS NOT NULL;

UPDATE akao_schedule s
SET target_chats = mapped.target_chats
FROM (
  SELECT
    s2.id,
    jsonb_agg(
      COALESCE(to_jsonb(lb.common_conversation_id::text), elem.value)
      ORDER BY elem.ordinality
    ) AS target_chats
  FROM akao_schedule s2
  CROSS JOIN LATERAL jsonb_array_elements(s2.target_chats) WITH ORDINALITY AS elem(value, ordinality)
  LEFT JOIN lark_base_chat_info lb
    ON elem.value #>> '{}' = lb.chat_id
  WHERE s2.target_chats IS NOT NULL
    AND jsonb_typeof(s2.target_chats) = 'array'
  GROUP BY s2.id
) mapped
WHERE s.id = mapped.id
  AND s.target_chats IS DISTINCT FROM mapped.target_chats;
