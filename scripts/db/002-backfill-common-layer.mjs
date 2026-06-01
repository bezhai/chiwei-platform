#!/usr/bin/env bun

import pg from 'pg';
import { v7 as uuidv7, validate as uuidValidate } from 'uuid';

const { Client } = pg;

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const REQUIRED_TABLES = [
  'conversation_messages',
  'agent_responses',
  'lark_base_chat_info',
  'lark_group_chat_info',
  'lark_user',
  'lark_user_open_id',
  'common_user',
  'common_conversation',
  'common_message',
  'common_agent_response',
  'lark_message',
];

function parseArgs(argv) {
  const options = {
    apply: false,
    batchSize: 1000,
    limit: null,
    skipResponses: false,
  };

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--apply') {
      options.apply = true;
    } else if (arg === '--dry-run') {
      options.apply = false;
    } else if (arg === '--skip-responses') {
      options.skipResponses = true;
    } else if (arg === '--batch-size') {
      options.batchSize = Number.parseInt(argv[++i] ?? '', 10);
    } else if (arg.startsWith('--batch-size=')) {
      options.batchSize = Number.parseInt(arg.slice('--batch-size='.length), 10);
    } else if (arg === '--limit') {
      options.limit = Number.parseInt(argv[++i] ?? '', 10);
    } else if (arg.startsWith('--limit=')) {
      options.limit = Number.parseInt(arg.slice('--limit='.length), 10);
    } else if (arg === '-h' || arg === '--help') {
      printUsage();
      process.exit(0);
    } else {
      throw new Error(`Unknown argument: ${arg}`);
    }
  }

  if (!Number.isInteger(options.batchSize) || options.batchSize < 1 || options.batchSize > 5000) {
    throw new Error('--batch-size must be an integer between 1 and 5000');
  }
  if (options.limit !== null && (!Number.isInteger(options.limit) || options.limit < 1)) {
    throw new Error('--limit must be a positive integer');
  }

  return options;
}

function printUsage() {
  console.log(`Usage:
  DATABASE_URL=postgres://user:pass@host:5432/db bun scripts/db/002-backfill-common-layer.mjs [--dry-run|--apply]

Options:
  --dry-run              Run every stage in transactions and roll back. This is the default.
  --apply                Commit the migration.
  --batch-size <n>       conversation_messages batch size, default 1000, max 5000.
  --limit <n>            Process at most n conversation_messages rows. Intended for rehearsal.
  --skip-responses       Skip agent_responses backfill.

The script connects directly to PostgreSQL. It never uses kubectl, pods,
services, or cluster-local port-forwarding.`);
}

function isUuid(value) {
  return typeof value === 'string' && UUID_RE.test(value) && uuidValidate(value);
}

function uuidFromLegacy(value) {
  return isUuid(value) ? value.toLowerCase() : null;
}

function jsonbValue(value) {
  return JSON.stringify(value ?? null);
}

function textOrNull(value) {
  if (value === undefined || value === null) {
    return null;
  }
  const text = String(value);
  return text.length === 0 ? null : text;
}

function parseJson(value) {
  if (typeof value !== 'string' || value.length === 0) {
    return null;
  }
  try {
    return JSON.parse(value);
  } catch {
    return null;
  }
}

function normalizeMessageContent(rawContent) {
  if (rawContent === null || rawContent === undefined) {
    return { content: [], contentText: null };
  }

  const parsed = parseJson(rawContent);
  if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
    const content = Array.isArray(parsed.items) ? parsed.items : [{ type: 'text', value: String(rawContent) }];
    const contentText = typeof parsed.text === 'string' ? parsed.text : String(rawContent);
    return { content, contentText };
  }
  if (Array.isArray(parsed)) {
    return { content: parsed, contentText: String(rawContent) };
  }

  return {
    content: [{ type: 'text', value: String(rawContent) }],
    contentText: String(rawContent),
  };
}

function scopeFromChatType(chatType) {
  return chatType === 'p2p' ? 'direct' : (chatType || 'group');
}

function buildAttachmentPolicy(row) {
  const policy = {
    source: 'lark',
  };
  if (row.download_has_permission_setting === 'not_anyone') {
    policy.download_allowed = false;
  } else if (row.download_has_permission_setting === 'all_members') {
    policy.download_allowed = true;
  }
  if (row.gray_config !== null && row.gray_config !== undefined) {
    policy.gray_config = row.gray_config;
  }
  return policy;
}

function makeValuesInsert(table, columns, rows, conflictSql) {
  const params = [];
  const valuesSql = rows.map((row) => {
    const slots = columns.map((column) => {
      params.push(row[column.name]);
      return `$${params.length}${column.cast ? `::${column.cast}` : ''}`;
    });
    return `(${slots.join(', ')})`;
  });
  const columnSql = columns.map((column) => column.name).join(', ');
  return {
    sql: `INSERT INTO ${table} (${columnSql}) VALUES ${valuesSql.join(', ')} ${conflictSql}`,
    params,
  };
}

async function queryOptionalRows(client, table, sql, params = []) {
  const exists = await tableExists(client, table);
  if (!exists) {
    return [];
  }
  return (await client.query(sql, params)).rows;
}

async function tableExists(client, table) {
  const result = await client.query('SELECT to_regclass($1) AS table_name', [table]);
  return result.rows[0]?.table_name !== null;
}

async function assertSchema(client) {
  const missing = [];
  for (const table of REQUIRED_TABLES) {
    if (!(await tableExists(client, table))) {
      missing.push(table);
    }
  }
  if (missing.length > 0) {
    throw new Error(`Missing required tables: ${missing.join(', ')}. Run 001-common-layer-schema.sql first.`);
  }
}

async function withTransaction(client, options, label, fn) {
  if (!options.apply) {
    const result = await fn();
    console.log(`[dry-run staged] ${label}`);
    return result;
  }

  await client.query('BEGIN');
  try {
    const result = await fn();
    await client.query('COMMIT');
    console.log(`[commit] ${label}`);
    return result;
  } catch (error) {
    await client.query('ROLLBACK');
    throw error;
  }
}

async function loadLegacyChatSources(client) {
  const rows = (await client.query(`
    SELECT DISTINCT
      cm.chat_id AS legacy_chat_id,
      COALESCE(
        lb.chat_id,
        ic2.channel_conversation_id,
        ic1.channel_chat_id,
        cm.chat_id
      ) AS lark_chat_id
    FROM conversation_messages cm
    LEFT JOIN lark_base_chat_info lb
      ON lb.chat_id = cm.chat_id
    LEFT JOIN identity_conversation_v2 ic2
      ON ic2.internal_conversation_id::text = cm.chat_id
    LEFT JOIN identity_chat ic1
      ON ic1.internal_chat_id = cm.chat_id
  `)).rows;

  const byLarkChatId = new Map();
  for (const row of rows) {
    if (!row.lark_chat_id) {
      continue;
    }
    if (!byLarkChatId.has(row.lark_chat_id)) {
      byLarkChatId.set(row.lark_chat_id, []);
    }
    byLarkChatId.get(row.lark_chat_id).push(row.legacy_chat_id);
  }
  return byLarkChatId;
}

async function migrateChats(client, options) {
  const byLarkChatId = await loadLegacyChatSources(client);
  if (byLarkChatId.size === 0) {
    return { legacyChatToCommon: new Map(), larkChatToCommon: new Map(), migrated: 0, skipped: 0 };
  }

  const larkChatIds = [...byLarkChatId.keys()];
  const rows = (await client.query(`
    SELECT
      lb.chat_id,
      lb.common_conversation_id,
      lb.chat_mode,
      lb.gray_config,
      g.name,
      g.avatar,
      g.user_count,
      g.is_leave,
      g.download_has_permission_setting
    FROM lark_base_chat_info lb
    LEFT JOIN lark_group_chat_info g
      ON g.chat_id = lb.chat_id
    WHERE lb.chat_id = ANY($1::text[])
  `, [larkChatIds])).rows;

  const larkChatToCommon = new Map();
  const legacyChatToCommon = new Map();
  const commonRows = [];
  const updateRows = [];
  const knownLarkChats = new Set(rows.map((row) => row.chat_id));

  for (const row of rows) {
    const commonConversationId = row.common_conversation_id ?? uuidv7();
    larkChatToCommon.set(row.chat_id, commonConversationId);
    for (const legacyChatId of byLarkChatId.get(row.chat_id) ?? []) {
      legacyChatToCommon.set(legacyChatId, commonConversationId);
    }

    commonRows.push({
      common_conversation_id: commonConversationId,
      channel: 'lark',
      scope: row.chat_mode === 'p2p' ? 'direct' : 'group',
      display_name: row.name ?? null,
      avatar_url: row.avatar ?? null,
      member_count: row.user_count ?? null,
      is_active: row.is_leave === null || row.is_leave === undefined ? true : !row.is_leave,
      attachment_policy: jsonbValue(buildAttachmentPolicy(row)),
    });
    updateRows.push({
      chat_id: row.chat_id,
      common_conversation_id: commonConversationId,
    });
  }

  let skipped = 0;
  for (const [larkChatId, legacyChatIds] of byLarkChatId.entries()) {
    if (knownLarkChats.has(larkChatId)) {
      continue;
    }
    skipped += legacyChatIds.length;
  }

  await withTransaction(client, options, 'chat mapping and common_conversation', async () => {
    if (commonRows.length > 0) {
      const insert = makeValuesInsert(
        'common_conversation',
        [
          { name: 'common_conversation_id' },
          { name: 'channel' },
          { name: 'scope' },
          { name: 'display_name' },
          { name: 'avatar_url' },
          { name: 'member_count' },
          { name: 'is_active' },
          { name: 'attachment_policy', cast: 'jsonb' },
        ],
        commonRows,
        `ON CONFLICT (common_conversation_id) DO UPDATE SET
          display_name = EXCLUDED.display_name,
          avatar_url = EXCLUDED.avatar_url,
          member_count = EXCLUDED.member_count,
          is_active = EXCLUDED.is_active,
          attachment_policy = EXCLUDED.attachment_policy,
          updated_at = now()`,
      );
      await client.query(insert.sql, insert.params);
    }

    for (const row of updateRows) {
      await client.query(`
        UPDATE lark_base_chat_info
        SET common_conversation_id = $1
        WHERE chat_id = $2
          AND common_conversation_id IS DISTINCT FROM $1
      `, [row.common_conversation_id, row.chat_id]);
    }
  });

  console.log(`[chats] lark=${rows.length} legacy=${legacyChatToCommon.size} skipped_legacy=${skipped}`);
  return { legacyChatToCommon, larkChatToCommon, migrated: rows.length, skipped };
}

async function loadIdentityUserMap(client) {
  const rows = await queryOptionalRows(client, 'identity_user_v2', `
    SELECT internal_user_id::text AS internal_user_id, channel_user_id
    FROM identity_user_v2
    WHERE channel = 'lark'
  `);
  return new Map(rows.map((row) => [row.internal_user_id, row.channel_user_id]));
}

async function migrateUsers(client, options) {
  const identityUserToChannelUser = await loadIdentityUserMap(client);
  const rows = (await client.query(`
    SELECT
      luo.app_id,
      luo.open_id,
      luo.union_id,
      luo.name AS open_name,
      luo.common_user_id,
      lu.name AS union_name,
      lu.avatar_origin
    FROM lark_user_open_id luo
    LEFT JOIN lark_user lu
      ON lu.union_id = luo.union_id
  `)).rows;

  const groupIdByKey = new Map();
  for (const row of rows) {
    const key = row.union_id ? `union:${row.union_id}` : `open:${row.app_id}:${row.open_id}`;
    const existing = groupIdByKey.get(key);
    if (!existing || row.common_user_id) {
      groupIdByKey.set(key, row.common_user_id ?? existing ?? uuidv7());
    }
  }

  const larkUserByOpenId = new Map();
  const larkUserByUnionId = new Map();
  const commonRows = [];
  const updateRows = [];
  const seenCommonUsers = new Set();

  for (const row of rows) {
    const key = row.union_id ? `union:${row.union_id}` : `open:${row.app_id}:${row.open_id}`;
    const commonUserId = groupIdByKey.get(key) ?? uuidv7();
    const displayName = row.union_name ?? row.open_name ?? null;

    larkUserByOpenId.set(row.open_id, commonUserId);
    if (row.union_id) {
      larkUserByUnionId.set(row.union_id, commonUserId);
    }
    if (!seenCommonUsers.has(commonUserId)) {
      commonRows.push({
        common_user_id: commonUserId,
        channel: 'lark',
        display_name: displayName,
        avatar_url: row.avatar_origin ?? null,
      });
      seenCommonUsers.add(commonUserId);
    }
    updateRows.push({
      app_id: row.app_id,
      open_id: row.open_id,
      common_user_id: commonUserId,
    });
  }

  const legacyUserRows = (await client.query(`
    SELECT user_id, max(username) AS username
    FROM conversation_messages
    WHERE role = 'user'
      AND user_id IS NOT NULL
      AND user_id <> ''
    GROUP BY user_id
  `)).rows;

  const legacyUserToCommon = new Map();
  const extraCommonRows = [];
  let unresolvedLegacyUsers = 0;

  for (const row of legacyUserRows) {
    const identityChannelUser = identityUserToChannelUser.get(row.user_id);
    const commonUserId =
      larkUserByUnionId.get(row.user_id) ??
      larkUserByOpenId.get(row.user_id) ??
      (identityChannelUser ? larkUserByUnionId.get(identityChannelUser) : null) ??
      (identityChannelUser ? larkUserByOpenId.get(identityChannelUser) : null) ??
      uuidFromLegacy(row.user_id);

    if (!commonUserId) {
      unresolvedLegacyUsers += 1;
      continue;
    }

    legacyUserToCommon.set(row.user_id, commonUserId);
    if (!seenCommonUsers.has(commonUserId)) {
      extraCommonRows.push({
        common_user_id: commonUserId,
        channel: 'lark',
        display_name: row.username ?? null,
        avatar_url: null,
      });
      seenCommonUsers.add(commonUserId);
    }
  }

  await withTransaction(client, options, 'user mapping and common_user', async () => {
    const allCommonRows = commonRows.concat(extraCommonRows);
    if (allCommonRows.length > 0) {
      const insert = makeValuesInsert(
        'common_user',
        [
          { name: 'common_user_id' },
          { name: 'channel' },
          { name: 'display_name' },
          { name: 'avatar_url' },
        ],
        allCommonRows,
        `ON CONFLICT (common_user_id) DO UPDATE SET
          display_name = COALESCE(EXCLUDED.display_name, common_user.display_name),
          avatar_url = COALESCE(EXCLUDED.avatar_url, common_user.avatar_url),
          updated_at = now()`,
      );
      await client.query(insert.sql, insert.params);
    }

    for (const row of updateRows) {
      await client.query(`
        UPDATE lark_user_open_id
        SET common_user_id = $1
        WHERE app_id = $2
          AND open_id = $3
          AND common_user_id IS DISTINCT FROM $1
      `, [row.common_user_id, row.app_id, row.open_id]);
    }
  });

  console.log(`[users] lark_open_ids=${rows.length} legacy=${legacyUserToCommon.size} unresolved_legacy=${unresolvedLegacyUsers}`);
  return { legacyUserToCommon, larkUserByOpenId, larkUserByUnionId };
}

async function resolveLegacyMessageSources(client, legacyIds) {
  const ids = [...new Set(legacyIds.filter(Boolean))];
  if (ids.length === 0) {
    return new Map();
  }

  const rows = (await client.query(`
    WITH source AS (
      SELECT unnest($1::text[]) AS legacy_message_id
    )
    SELECT
      source.legacy_message_id,
      COALESCE(
        im2.channel_message_id,
        im1.channel_message_id,
        CASE WHEN source.legacy_message_id LIKE 'om_%' THEN source.legacy_message_id END
      ) AS om_id,
      lm.common_message_id
    FROM source
    LEFT JOIN identity_message_v2 im2
      ON im2.internal_message_id::text = source.legacy_message_id
    LEFT JOIN identity_message im1
      ON im1.internal_message_id = source.legacy_message_id
    LEFT JOIN lark_message lm
      ON lm.om_id = COALESCE(
        im2.channel_message_id,
        im1.channel_message_id,
        CASE WHEN source.legacy_message_id LIKE 'om_%' THEN source.legacy_message_id END
      )
  `, [ids])).rows;

  const result = new Map();
  for (const row of rows) {
    result.set(row.legacy_message_id, {
      omId: row.om_id ?? null,
      existingCommonMessageId: row.common_message_id ?? null,
    });
  }
  return result;
}

function assignCommonMessageIds(sources) {
  const omAssignments = new Map();
  const result = new Map();

  for (const [legacyMessageId, source] of sources.entries()) {
    let commonMessageId = source.existingCommonMessageId ?? null;
    if (!commonMessageId && source.omId) {
      commonMessageId = omAssignments.get(source.omId) ?? null;
      if (!commonMessageId) {
        commonMessageId = uuidFromLegacy(legacyMessageId) ?? uuidv7();
        omAssignments.set(source.omId, commonMessageId);
      }
    }
    if (!commonMessageId) {
      commonMessageId = uuidFromLegacy(legacyMessageId);
    }
    if (commonMessageId) {
      result.set(legacyMessageId, {
        commonMessageId,
        omId: source.omId,
      });
    }
  }

  return result;
}

async function loadConversationMessageBatch(client, cursor, batchSize, remaining) {
  const limit = remaining === null ? batchSize : Math.min(batchSize, remaining);
  if (limit <= 0) {
    return [];
  }

  const result = await client.query(`
    SELECT
      cm.message_id,
      cm.user_id,
      cm.username,
      cm.content,
      cm.role,
      cm.root_message_id,
      cm.reply_message_id,
      cm.chat_id,
      cm.chat_type,
      cm.create_time,
      cm.message_type,
      cm.bot_name,
      cm.response_id,
      COALESCE(
        im2.channel_message_id,
        im1.channel_message_id,
        CASE WHEN cm.message_id LIKE 'om_%' THEN cm.message_id END
      ) AS om_id,
      COALESCE(
        ic2.channel_conversation_id,
        ic1.channel_chat_id,
        cm.chat_id
      ) AS lark_chat_id
    FROM conversation_messages cm
    LEFT JOIN identity_message_v2 im2
      ON im2.internal_message_id::text = cm.message_id
    LEFT JOIN identity_message im1
      ON im1.internal_message_id = cm.message_id
    LEFT JOIN identity_conversation_v2 ic2
      ON ic2.internal_conversation_id::text = cm.chat_id
    LEFT JOIN identity_chat ic1
      ON ic1.internal_chat_id = cm.chat_id
    WHERE (
      $1::bigint IS NULL
      OR cm.create_time > $1::bigint
      OR (cm.create_time = $1::bigint AND cm.message_id > $2::text)
    )
    ORDER BY cm.create_time ASC, cm.message_id ASC
    LIMIT $3
  `, [cursor.createTime, cursor.messageId, limit]);

  return result.rows;
}

async function insertCommonMessages(client, rows) {
  if (rows.length === 0) {
    return;
  }
  const insert = makeValuesInsert(
    'common_message',
    [
      { name: 'common_message_id' },
      { name: 'channel' },
      { name: 'common_conversation_id' },
      { name: 'common_user_id' },
      { name: 'sender_display_name' },
      { name: 'role' },
      { name: 'content', cast: 'jsonb' },
      { name: 'content_text' },
      { name: 'common_root_message_id' },
      { name: 'common_reply_message_id' },
      { name: 'scope' },
      { name: 'message_type' },
      { name: 'bot_name' },
      { name: 'response_id' },
      { name: 'event_time' },
    ],
    rows,
    'ON CONFLICT (common_message_id) DO NOTHING',
  );
  await client.query(insert.sql, insert.params);
}

async function insertLarkMessages(client, rows) {
  if (rows.length === 0) {
    return;
  }
  const insert = makeValuesInsert(
    'lark_message',
    [
      { name: 'om_id' },
      { name: 'common_message_id' },
      { name: 'chat_id' },
      { name: 'sender_union_id' },
      { name: 'root_om_id' },
      { name: 'reply_om_id' },
      { name: 'message_type' },
      { name: 'raw_event', cast: 'jsonb' },
    ],
    rows,
    'ON CONFLICT (om_id) DO NOTHING',
  );
  await client.query(insert.sql, insert.params);
}

async function migrateMessages(client, options, chatMaps, userMaps) {
  let cursor = { createTime: null, messageId: '' };
  let processed = 0;
  let inserted = 0;
  let skippedMissingChat = 0;
  let skippedNoStableMessageId = 0;
  let unresolvedUsers = 0;

  while (options.limit === null || processed < options.limit) {
    const remaining = options.limit === null ? null : options.limit - processed;
    const batch = await loadConversationMessageBatch(client, cursor, options.batchSize, remaining);
    if (batch.length === 0) {
      break;
    }

    const legacyIds = [];
    for (const row of batch) {
      legacyIds.push(row.message_id, row.root_message_id, row.reply_message_id);
    }
    const sourceMap = await resolveLegacyMessageSources(client, legacyIds);
    for (const row of batch) {
      if (!sourceMap.has(row.message_id)) {
        sourceMap.set(row.message_id, { omId: row.om_id ?? null, existingCommonMessageId: null });
      }
    }
    const idMap = assignCommonMessageIds(sourceMap);

    const commonRows = [];
    const larkRows = [];
    for (const row of batch) {
      const assigned = idMap.get(row.message_id);
      if (!assigned) {
        skippedNoStableMessageId += 1;
        continue;
      }

      const commonConversationId =
        chatMaps.legacyChatToCommon.get(row.chat_id) ??
        chatMaps.larkChatToCommon.get(row.lark_chat_id);
      if (!commonConversationId) {
        skippedMissingChat += 1;
        continue;
      }

      let commonUserId = null;
      if (row.role === 'user') {
        commonUserId =
          userMaps.legacyUserToCommon.get(row.user_id) ??
          userMaps.larkUserByUnionId.get(row.user_id) ??
          userMaps.larkUserByOpenId.get(row.user_id) ??
          uuidFromLegacy(row.user_id);
        if (!commonUserId) {
          unresolvedUsers += 1;
        }
      }

      const rootAssigned = idMap.get(row.root_message_id);
      const replyAssigned = idMap.get(row.reply_message_id);
      const rootCommonMessageId = rootAssigned?.commonMessageId ?? assigned.commonMessageId;
      const { content, contentText } = normalizeMessageContent(row.content);

      commonRows.push({
        common_message_id: assigned.commonMessageId,
        channel: 'lark',
        common_conversation_id: commonConversationId,
        common_user_id: commonUserId,
        sender_display_name: row.username ?? null,
        role: row.role,
        content: jsonbValue(content),
        content_text: contentText,
        common_root_message_id: rootCommonMessageId,
        common_reply_message_id: replyAssigned?.commonMessageId ?? null,
        scope: scopeFromChatType(row.chat_type),
        message_type: row.message_type ?? 'text',
        bot_name: row.bot_name ?? null,
        response_id: row.response_id ?? null,
        event_time: row.create_time,
      });

      if (assigned.omId) {
        larkRows.push({
          om_id: assigned.omId,
          common_message_id: assigned.commonMessageId,
          chat_id: row.lark_chat_id,
          sender_union_id: row.role === 'user' ? textOrNull(row.user_id) : null,
          root_om_id: rootAssigned?.omId ?? null,
          reply_om_id: replyAssigned?.omId ?? null,
          message_type: row.message_type ?? 'text',
          raw_event: jsonbValue(null),
        });
      }
    }

    await withTransaction(client, options, `conversation_messages batch after ${cursor.createTime ?? 'start'}/${cursor.messageId || '-'}`, async () => {
      await insertCommonMessages(client, commonRows);
      await insertLarkMessages(client, larkRows);
    });

    processed += batch.length;
    inserted += commonRows.length;
    const last = batch[batch.length - 1];
    cursor = { createTime: last.create_time, messageId: last.message_id };
    console.log(`[messages] processed=${processed} staged=${inserted} skipped_missing_chat=${skippedMissingChat} skipped_no_stable_id=${skippedNoStableMessageId}`);
  }

  console.log(`[messages done] processed=${processed} staged=${inserted} skipped_missing_chat=${skippedMissingChat} skipped_no_stable_id=${skippedNoStableMessageId} unresolved_users=${unresolvedUsers}`);
  return { processed, inserted, skippedMissingChat, skippedNoStableMessageId, unresolvedUsers };
}

async function loadAgentResponseBatch(client, cursor, batchSize) {
  const result = await client.query(`
    SELECT
      ar.id::text AS id,
      ar.session_id,
      ar.trigger_message_id,
      ar.chat_id,
      ar.bot_name,
      ar.persona_id,
      ar.response_type,
      ar.replies,
      ar.response_text,
      ar.agent_metadata,
      ar.safety_status,
      ar.safety_result,
      ar.status,
      ar.created_at,
      ar.updated_at
    FROM agent_responses ar
    WHERE (
      $1::timestamptz IS NULL
      OR ar.created_at > $1::timestamptz
      OR (ar.created_at = $1::timestamptz AND ar.session_id > $2::text)
    )
    ORDER BY ar.created_at ASC, ar.session_id ASC
    LIMIT $3
  `, [cursor.createdAt, cursor.sessionId, batchSize]);
  return result.rows;
}

function normalizeReplies(replies) {
  if (Array.isArray(replies)) {
    return replies;
  }
  if (typeof replies === 'string') {
    const parsed = parseJson(replies);
    return Array.isArray(parsed) ? parsed : [];
  }
  return [];
}

async function migrateAgentResponses(client, options, chatMaps) {
  let cursor = { createdAt: null, sessionId: '' };
  let processed = 0;
  let staged = 0;
  let skipped = 0;

  while (true) {
    const batch = await loadAgentResponseBatch(client, cursor, options.batchSize);
    if (batch.length === 0) {
      break;
    }

    const legacyMessageIds = [];
    for (const row of batch) {
      legacyMessageIds.push(row.trigger_message_id);
      for (const reply of normalizeReplies(row.replies)) {
        legacyMessageIds.push(reply.message_id, reply.common_message_id);
      }
    }

    const sourceMap = await resolveLegacyMessageSources(client, legacyMessageIds);
    const idMap = assignCommonMessageIds(sourceMap);
    const responseRows = [];

    for (const row of batch) {
      const trigger = idMap.get(row.trigger_message_id);
      const commonConversationId = chatMaps.legacyChatToCommon.get(row.chat_id);
      if (!trigger || !commonConversationId) {
        skipped += 1;
        continue;
      }

      const replies = normalizeReplies(row.replies)
        .map((reply) => {
          const legacyMessageId = reply.common_message_id ?? reply.message_id;
          const assigned = idMap.get(legacyMessageId);
          if (!assigned) {
            return null;
          }
          return {
            common_message_id: assigned.commonMessageId,
            content_type: reply.content_type,
            sent_at: reply.sent_at,
          };
        })
        .filter(Boolean);

      responseRows.push({
        response_id: uuidFromLegacy(row.id) ?? uuidv7(),
        session_id: row.session_id,
        trigger_common_message_id: trigger.commonMessageId,
        common_conversation_id: commonConversationId,
        bot_name: row.bot_name ?? null,
        persona_id: row.persona_id ?? null,
        response_type: row.response_type ?? 'reply',
        replies: jsonbValue(replies),
        response_text: row.response_text ?? null,
        agent_metadata: jsonbValue(row.agent_metadata ?? {}),
        safety_status: row.safety_status ?? 'pending',
        safety_result: row.safety_result === null || row.safety_result === undefined ? null : jsonbValue(row.safety_result),
        status: row.status ?? 'pending',
        created_at: row.created_at,
        updated_at: row.updated_at,
      });
    }

    await withTransaction(client, options, `agent_responses batch after ${cursor.createdAt ?? 'start'}/${cursor.sessionId || '-'}`, async () => {
      if (responseRows.length === 0) {
        return;
      }
      const insert = makeValuesInsert(
        'common_agent_response',
        [
          { name: 'response_id' },
          { name: 'session_id' },
          { name: 'trigger_common_message_id' },
          { name: 'common_conversation_id' },
          { name: 'bot_name' },
          { name: 'persona_id' },
          { name: 'response_type' },
          { name: 'replies', cast: 'jsonb' },
          { name: 'response_text' },
          { name: 'agent_metadata', cast: 'jsonb' },
          { name: 'safety_status' },
          { name: 'safety_result', cast: 'jsonb' },
          { name: 'status' },
          { name: 'created_at' },
          { name: 'updated_at' },
        ],
        responseRows,
        `ON CONFLICT (session_id) DO UPDATE SET
          trigger_common_message_id = EXCLUDED.trigger_common_message_id,
          common_conversation_id = EXCLUDED.common_conversation_id,
          bot_name = EXCLUDED.bot_name,
          persona_id = EXCLUDED.persona_id,
          response_type = EXCLUDED.response_type,
          replies = EXCLUDED.replies,
          response_text = EXCLUDED.response_text,
          agent_metadata = EXCLUDED.agent_metadata,
          safety_status = EXCLUDED.safety_status,
          safety_result = EXCLUDED.safety_result,
          status = EXCLUDED.status,
          updated_at = EXCLUDED.updated_at`,
      );
      await client.query(insert.sql, insert.params);
    });

    processed += batch.length;
    staged += responseRows.length;
    const last = batch[batch.length - 1];
    cursor = { createdAt: last.created_at, sessionId: last.session_id };
    console.log(`[responses] processed=${processed} staged=${staged} skipped=${skipped}`);
  }

  console.log(`[responses done] processed=${processed} staged=${staged} skipped=${skipped}`);
  return { processed, staged, skipped };
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const mode = options.apply ? 'APPLY' : 'DRY RUN';
  console.log(`[mode] ${mode}`);
  console.log(`[uuid] using uuid.v7() from package uuid`);

  const client = new Client({
    connectionString: process.env.DATABASE_URL,
  });

  await client.connect();
  try {
    await assertSchema(client);
    if (!options.apply) {
      await client.query('BEGIN');
    }
    const chatMaps = await migrateChats(client, options);
    const userMaps = await migrateUsers(client, options);
    await migrateMessages(client, options, chatMaps, userMaps);
    if (!options.skipResponses) {
      await migrateAgentResponses(client, options, chatMaps);
    }
    if (!options.apply) {
      await client.query('ROLLBACK');
      console.log('[dry-run rollback] entire migration');
    }
  } catch (error) {
    if (!options.apply) {
      try {
        await client.query('ROLLBACK');
      } catch {
        // The connection may already be closed or outside a transaction.
      }
    }
    throw error;
  } finally {
    await client.end();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
