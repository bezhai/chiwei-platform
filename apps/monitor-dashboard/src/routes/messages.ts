import { Hono } from 'hono';
import { AppDataSource, CommonConversation, CommonMessage, CommonUser } from '../db';

const app = new Hono();

export const P2P_NAME_SQL = `SELECT DISTINCT ON (cm.common_conversation_id)
         cm.common_conversation_id AS chat_id,
         cm.sender_display_name AS user_name
       FROM common_message cm
       WHERE cm.common_conversation_id = ANY($1::uuid[])
         AND cm.role = 'user'
         AND cm.sender_display_name IS NOT NULL
       ORDER BY cm.common_conversation_id, cm.event_time DESC`;

const parseNumber = (value: string | undefined, defaultValue: number) => {
  if (!value) {
    return defaultValue;
  }
  const parsed = Number(value);
  return Number.isNaN(parsed) ? defaultValue : parsed;
};

app.get('/api/messages', async (c) => {
  const page = Math.max(1, parseNumber(c.req.query('page'), 1));
  const pageSize = Math.min(100, Math.max(1, parseNumber(c.req.query('pageSize'), 20)));
  const chatId = c.req.query('chatId') || '';
  const userId = c.req.query('userId') || '';
  const role = c.req.query('role') || '';
  const botName = c.req.query('botName') || '';
  const startTime = c.req.query('startTime') || '';
  const endTime = c.req.query('endTime') || '';
  const rootMessageId = c.req.query('rootMessageId') || '';
  const replyMessageId = c.req.query('replyMessageId') || '';
  const messageType = c.req.query('messageType') || '';

  const repo = AppDataSource.getRepository(CommonMessage);
  const qb = repo
    .createQueryBuilder('msg')
    .select([
      'msg.common_message_id AS message_id',
      'msg.common_user_id AS user_id',
      'msg.sender_display_name AS username',
      'msg.content AS content',
      'msg.content_text AS content_text',
      'msg.role AS role',
      'msg.common_root_message_id AS root_message_id',
      'msg.common_reply_message_id AS reply_message_id',
      'msg.common_conversation_id AS chat_id',
      `CASE WHEN msg.scope = 'direct' THEN 'p2p' ELSE msg.scope END AS chat_type`,
      'msg.event_time AS create_time',
      'msg.message_type AS message_type',
      'msg.bot_name AS bot_name',
      'msg.response_id AS response_id',
      `CASE WHEN msg.role = 'assistant' THEN '赤尾' ELSE msg.sender_display_name END AS user_name`,
      'cc.display_name AS group_name',
    ])
    .leftJoin(
      'common_conversation',
      'cc',
      'msg.common_conversation_id = cc.common_conversation_id'
    );

  if (chatId) {
    qb.andWhere('msg.common_conversation_id = :chatId', { chatId });
  }
  if (userId) {
    qb.andWhere('msg.common_user_id = :userId', { userId });
  }
  if (role) {
    qb.andWhere('msg.role = :role', { role });
  }
  if (botName) {
    qb.andWhere('msg.bot_name = :botName', { botName });
  }
  if (startTime) {
    qb.andWhere('msg.event_time >= :startTime', { startTime });
  }
  if (endTime) {
    qb.andWhere('msg.event_time <= :endTime', { endTime });
  }
  if (rootMessageId) {
    qb.andWhere('msg.common_root_message_id = :rootMessageId', { rootMessageId });
  }
  if (replyMessageId) {
    qb.andWhere('msg.common_reply_message_id = :replyMessageId', { replyMessageId });
  }
  if (messageType) {
    qb.andWhere('msg.message_type = :messageType', { messageType });
  }

  const countResult = await qb
    .clone()
    .select('COUNT(*)', 'count')
    .getRawOne();
  const total = parseInt(countResult?.count ?? '0', 10);

  qb.orderBy('msg.event_time', 'DESC');
  qb.offset((page - 1) * pageSize).limit(pageSize);
  const rows = await qb.getRawMany();

  const p2pChatIds = [
    ...new Set(
      rows
        .filter((r) => r.chat_type === 'p2p')
        .map((r) => r.chat_id)
    ),
  ];

  let p2pNameMap: Record<string, string> = {};
  if (p2pChatIds.length > 0) {
    const p2pRows: { chat_id: string; user_name: string }[] = await AppDataSource.query(
      P2P_NAME_SQL,
      [p2pChatIds]
    );
    for (const r of p2pRows) {
      p2pNameMap[r.chat_id] = r.user_name;
    }
  }

  const data = rows.map((row) => {
    let chat_name: string;
    if (row.chat_type === 'group') {
      chat_name = row.group_name || row.chat_id;
    } else {
      const userName = p2pNameMap[row.chat_id];
      chat_name = userName ? `和${userName}的私聊会话` : row.chat_id;
    }
    const { group_name, ...rest } = row;
    return { ...rest, chat_name };
  });

  return c.json({
    data,
    total,
    page,
    pageSize,
  });
});

app.get('/api/chats', async (c) => {
  const keyword = (c.req.query('keyword') || '').trim();
  const repo = AppDataSource.getRepository(CommonConversation);
  const qb = repo
    .createQueryBuilder('cc')
    .select(['cc.common_conversation_id AS chat_id', 'cc.display_name AS name'])
    .where("cc.scope = 'group'");
  if (keyword) {
    qb.andWhere('cc.display_name ILIKE :kw', { kw: `%${keyword}%` });
  }
  qb.orderBy('cc.display_name', 'ASC').limit(30);
  return c.json(await qb.getRawMany());
});

app.get('/api/users', async (c) => {
  const keyword = (c.req.query('keyword') || '').trim();
  const repo = AppDataSource.getRepository(CommonUser);
  const qb = repo.createQueryBuilder('u').select(['u.common_user_id AS user_id', 'u.display_name AS name']);
  if (keyword) {
    qb.where('u.display_name ILIKE :kw', { kw: `%${keyword}%` });
  }
  qb.orderBy('u.display_name', 'ASC').limit(30);
  return c.json(await qb.getRawMany());
});

export default app;
