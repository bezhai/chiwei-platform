import Router from '@koa/router';
import { AppDataSource, ConversationMessage } from '../db';

const router = new Router();

const parseNumber = (value: string | undefined, defaultValue: number) => {
  if (!value) {
    return defaultValue;
  }
  const parsed = Number(value);
  return Number.isNaN(parsed) ? defaultValue : parsed;
};

router.get('/api/messages', async (ctx) => {
  const page = Math.max(1, parseNumber(ctx.query.page as string | undefined, 1));
  const pageSize = Math.min(100, Math.max(1, parseNumber(ctx.query.pageSize as string | undefined, 20)));
  const chatId = (ctx.query.chatId as string | undefined) || '';
  const userId = (ctx.query.userId as string | undefined) || '';
  const role = (ctx.query.role as string | undefined) || '';
  const botName = (ctx.query.botName as string | undefined) || '';
  const startTime = (ctx.query.startTime as string | undefined) || '';
  const endTime = (ctx.query.endTime as string | undefined) || '';
  const rootMessageId = (ctx.query.rootMessageId as string | undefined) || '';
  const replyMessageId = (ctx.query.replyMessageId as string | undefined) || '';
  const messageType = (ctx.query.messageType as string | undefined) || '';

  const repo = AppDataSource.getRepository(ConversationMessage);
  const qb = repo
    .createQueryBuilder('msg')
    .select([
      'msg.*',
      `CASE WHEN msg.role = 'assistant' THEN '赤尾' ELSE COALESCE(lu.name, msg.user_id) END AS user_name`,
      'gc.name AS group_name',
    ])
    .leftJoin('lark_user', 'lu', 'msg.user_id = lu.union_id')
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
      `SELECT DISTINCT ON (cm.chat_id)
         cm.chat_id,
         COALESCE(lu.name, cm.user_id) AS user_name
       FROM conversation_messages cm
       LEFT JOIN lark_user lu ON cm.user_id = lu.union_id
       WHERE cm.chat_id = ANY($1) AND cm.role = 'user'
       ORDER BY cm.chat_id, cm.create_time DESC`,
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

  ctx.body = {
    data,
    total,
    page,
    pageSize,
  };
});

export default router;
