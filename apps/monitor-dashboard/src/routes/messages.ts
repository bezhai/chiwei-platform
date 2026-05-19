import { Hono } from 'hono';
import { AppDataSource, ConversationMessage, LarkUser, LarkGroupChatInfo } from '../db';

const app = new Hono();

// 身份全局化：p2p 会话名取自 conversation_messages.username 冗余列。
// DISTINCT ON (chat_id) + ORDER BY chat_id, create_time DESC 取每个会话
// 最新一条 user 消息的名字；最新一行 username 可能为空（拉不到发送者名
// 时落 null，不写脏占位），必须 username IS NOT NULL 过滤，否则 DISTINCT
// ON 选中空行会丢掉本来更早可用的名字。
export const P2P_NAME_SQL = `SELECT DISTINCT ON (cm.chat_id)
         cm.chat_id,
         cm.username AS user_name
       FROM conversation_messages cm
       WHERE cm.chat_id = ANY($1)
         AND cm.role = 'user'
         AND cm.username IS NOT NULL
       ORDER BY cm.chat_id, cm.create_time DESC`;

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

  const repo = AppDataSource.getRepository(ConversationMessage);
  const qb = repo
    .createQueryBuilder('msg')
    .select([
      'msg.*',
      // 身份全局化：不再 JOIN lark_user 取名，直接读 conversation_messages
      // .username 冗余列（无 fallback）。assistant 行显示名仍按 role 派生。
      `CASE WHEN msg.role = 'assistant' THEN '赤尾' ELSE msg.username END AS user_name`,
      'gc.name AS group_name',
    ])
    .leftJoin('lark_group_chat_info', 'gc', 'msg.chat_id = gc.chat_id');

  if (chatId) {
    qb.andWhere('msg.chat_id = :chatId', { chatId });
  }
  if (userId) {
    qb.andWhere('msg.user_id = :userId', { userId });
  }
  if (role) {
    qb.andWhere('msg.role = :role', { role });
  }
  if (botName) {
    qb.andWhere('msg.bot_name = :botName', { botName });
  }
  if (startTime) {
    qb.andWhere('msg.create_time >= :startTime', { startTime });
  }
  if (endTime) {
    qb.andWhere('msg.create_time <= :endTime', { endTime });
  }
  if (rootMessageId) {
    qb.andWhere('msg.root_message_id = :rootMessageId', { rootMessageId });
  }
  if (replyMessageId) {
    qb.andWhere('msg.reply_message_id = :replyMessageId', { replyMessageId });
  }
  if (messageType) {
    qb.andWhere('msg.message_type = :messageType', { messageType });
  }

  const countResult = await qb
    .clone()
    .select('COUNT(*)', 'count')
    .getRawOne();
  const total = parseInt(countResult?.count ?? '0', 10);

  qb.orderBy('msg.create_time', 'DESC');
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
  const repo = AppDataSource.getRepository(LarkGroupChatInfo);
  const qb = repo.createQueryBuilder('gc').select(['gc.chat_id AS chat_id', 'gc.name AS name']);
  if (keyword) {
    qb.where('gc.name ILIKE :kw', { kw: `%${keyword}%` });
  }
  qb.orderBy('gc.name', 'ASC').limit(30);
  return c.json(await qb.getRawMany());
});

app.get('/api/users', async (c) => {
  const keyword = (c.req.query('keyword') || '').trim();
  const repo = AppDataSource.getRepository(LarkUser);
  const qb = repo.createQueryBuilder('u').select(['u.union_id AS user_id', 'u.name AS name']);
  if (keyword) {
    qb.where('u.name ILIKE :kw', { kw: `%${keyword}%` });
  }
  qb.orderBy('u.name', 'ASC').limit(30);
  return c.json(await qb.getRawMany());
});

export default app;
