import Router from '@koa/router';
import { AppDataSource, ConversationMessage, LarkGroupChatInfo, DiaryEntry, WeeklyReview } from '../db';

const router = new Router();

function msAgo(days: number): string {
  return String(Date.now() - days * 86400000);
}

function todayStartMs(): string {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return String(d.getTime());
}

function msToDateStr(ms: string | number): string {
  return new Date(Number(ms)).toISOString().slice(0, 10);
}

/** GET /api/activity/overview — 群活跃度概览 */
router.get('/api/activity/overview', async (ctx) => {
  const days = Number(ctx.query.days) || 7;
  const since = msAgo(days);
  const todayMs = todayStartMs();

  const repo = AppDataSource.getRepository(ConversationMessage);

  // 4 simple count queries
  const [periodTotal, todayTotal, todayBotReplies, todayActiveGroups] = await Promise.all([
    repo.createQueryBuilder('cm')
      .where('cm.create_time >= :since', { since })
      .getCount(),

    repo.createQueryBuilder('cm')
      .where('cm.create_time >= :todayMs', { todayMs })
      .getCount(),

    repo.createQueryBuilder('cm')
      .where('cm.create_time >= :todayMs', { todayMs })
      .andWhere('cm.role = :role', { role: 'assistant' })
      .getCount(),

    repo.createQueryBuilder('cm')
      .select('cm.chat_id')
      .where('cm.create_time >= :todayMs', { todayMs })
      .groupBy('cm.chat_id')
      .getCount(),
  ]);

  // Fetch raw rows, aggregate in JS
  const rows = await repo
    .createQueryBuilder('cm')
    .select(['cm.chat_id', 'cm.create_time', 'cm.role'])
    .where('cm.create_time >= :since', { since })
    .getMany();

  // Group name lookup
  const chatIds = [...new Set(rows.map((r) => r.chat_id))];
  const groupInfos = chatIds.length > 0
    ? await AppDataSource.getRepository(LarkGroupChatInfo)
        .createQueryBuilder('g')
        .where('g.chat_id IN (:...chatIds)', { chatIds })
        .getMany()
    : [];
  const nameMap = new Map(groupInfos.map((g) => [g.chat_id, g.name]));

  // Aggregate
  const groupMap = new Map<string, {
    chat_id: string;
    group_name: string;
    message_count: number;
    bot_replies: number;
    dailyMap: Map<string, number>;
  }>();

  for (const row of rows) {
    let group = groupMap.get(row.chat_id);
    if (!group) {
      group = {
        chat_id: row.chat_id,
        group_name: nameMap.get(row.chat_id) || row.chat_id,
        message_count: 0,
        bot_replies: 0,
        dailyMap: new Map(),
      };
      groupMap.set(row.chat_id, group);
    }
    group.message_count++;
    if (row.role === 'assistant') group.bot_replies++;
    const dateStr = msToDateStr(row.create_time);
    group.dailyMap.set(dateStr, (group.dailyMap.get(dateStr) || 0) + 1);
  }

  const groups = [...groupMap.values()]
    .sort((a, b) => b.message_count - a.message_count)
    .map(({ dailyMap, ...rest }) => ({
      ...rest,
      daily_counts: [...dailyMap.entries()]
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([date, count]) => ({ date, count })),
    }));

  ctx.body = {
    summary: {
      period_total: periodTotal,
      today_total: todayTotal,
      today_bot_replies: todayBotReplies,
      today_active_groups: todayActiveGroups,
    },
    groups,
  };
});

/** GET /api/activity/diary-status — 日记/周记生成状态 */
router.get('/api/activity/diary-status', async (ctx) => {
  const diaryRepo = AppDataSource.getRepository(DiaryEntry);
  const weeklyRepo = AppDataSource.getRepository(WeeklyReview);

  const sevenDaysAgo = new Date();
  sevenDaysAgo.setDate(sevenDaysAgo.getDate() - 7);
  const sevenDaysAgoStr = sevenDaysAgo.toISOString().slice(0, 10);

  const fourWeeksAgo = new Date();
  fourWeeksAgo.setDate(fourWeeksAgo.getDate() - 28);
  const fourWeeksAgoStr = fourWeeksAgo.toISOString().slice(0, 10);

  // Fetch all diary entries and aggregate in JS
  const diaries = await diaryRepo
    .createQueryBuilder('de')
    .select(['de.chat_id', 'de.diary_date'])
    .getMany();

  const diaryMap = new Map<string, { latest_diary_date: string; diary_count_7d: number }>();
  for (const d of diaries) {
    const existing = diaryMap.get(d.chat_id);
    const isRecent = d.diary_date >= sevenDaysAgoStr;
    if (!existing) {
      diaryMap.set(d.chat_id, {
        latest_diary_date: d.diary_date,
        diary_count_7d: isRecent ? 1 : 0,
      });
    } else {
      if (d.diary_date > existing.latest_diary_date) existing.latest_diary_date = d.diary_date;
      if (isRecent) existing.diary_count_7d++;
    }
  }

  // Fetch all weekly reviews and aggregate in JS
  const weeklies = await weeklyRepo
    .createQueryBuilder('wr')
    .select(['wr.chat_id', 'wr.week_start'])
    .getMany();

  const weeklyMap = new Map<string, { latest_week_start: string; review_count_4w: number }>();
  for (const w of weeklies) {
    const existing = weeklyMap.get(w.chat_id);
    const isRecent = w.week_start >= fourWeeksAgoStr;
    if (!existing) {
      weeklyMap.set(w.chat_id, {
        latest_week_start: w.week_start,
        review_count_4w: isRecent ? 1 : 0,
      });
    } else {
      if (w.week_start > existing.latest_week_start) existing.latest_week_start = w.week_start;
      if (isRecent) existing.review_count_4w++;
    }
  }

  ctx.body = {
    diary: [...diaryMap.entries()].map(([chat_id, v]) => ({ chat_id, ...v })),
    weekly: [...weeklyMap.entries()].map(([chat_id, v]) => ({ chat_id, ...v })),
  };
});

export default router;
