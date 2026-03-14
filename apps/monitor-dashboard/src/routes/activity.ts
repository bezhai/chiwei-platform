import Router from '@koa/router';
import { AppDataSource } from '../db';
import { paasClient } from '../paas-client';

const router = new Router();

/** GET /api/activity/overview — 群活跃度概览 */
router.get('/api/activity/overview', async (ctx) => {
  const days = Number(ctx.query.days) || 7;

  // All data from chiwei DB via conversation_messages + lark_group_chat_info
  const summary = await AppDataSource.query(
    `SELECT
       COUNT(*) FILTER (WHERE cm.created_at >= NOW() - INTERVAL '1 day') AS today_total,
       COUNT(*) FILTER (WHERE cm.created_at >= NOW() - INTERVAL '1 day' AND cm.role = 'assistant') AS today_bot_replies,
       COUNT(DISTINCT cm.chat_id) FILTER (WHERE cm.created_at >= NOW() - INTERVAL '1 day') AS today_active_groups,
       COUNT(*) FILTER (WHERE cm.created_at >= NOW() - make_interval(days => $1)) AS period_total
     FROM conversation_messages cm
     WHERE cm.created_at >= NOW() - make_interval(days => $1)`,
    [days],
  );

  // Per-group stats with daily breakdown for sparkline
  const groupStats = await AppDataSource.query(
    `SELECT
       cm.chat_id,
       COALESCE(g.name, cm.chat_id) AS group_name,
       COUNT(*) AS message_count,
       COUNT(*) FILTER (WHERE cm.role = 'assistant') AS bot_replies,
       json_agg(
         json_build_object('date', d.date::text, 'count', COALESCE(dc.cnt, 0))
         ORDER BY d.date
       ) AS daily_counts
     FROM conversation_messages cm
     LEFT JOIN lark_group_chat_info g ON g.chat_id = cm.chat_id
     CROSS JOIN LATERAL (
       SELECT generate_series(
         (NOW() - make_interval(days => $1))::date,
         NOW()::date,
         '1 day'::interval
       )::date AS date
     ) d
     LEFT JOIN LATERAL (
       SELECT COUNT(*) AS cnt
       FROM conversation_messages cm2
       WHERE cm2.chat_id = cm.chat_id
         AND cm2.created_at::date = d.date
     ) dc ON true
     WHERE cm.created_at >= NOW() - make_interval(days => $1)
     GROUP BY cm.chat_id, g.name
     ORDER BY message_count DESC`,
    [days],
  );

  ctx.body = {
    summary: summary[0] || {},
    groups: groupStats,
  };
});

/** GET /api/activity/diary-status — 日记/周记生成状态（通过 PaaS ops/query 跨库查询） */
router.get('/api/activity/diary-status', async (ctx) => {
  try {
    // Query diary_entry from agent-service DB
    const diaryData = await paasClient.post('/api/paas/ops/query', {
      db: 'chiwei',
      sql: `SELECT
              de.chat_id,
              MAX(de.target_date) AS latest_diary_date,
              COUNT(*) FILTER (WHERE de.target_date >= CURRENT_DATE - 7) AS diary_count_7d
            FROM diary_entry de
            GROUP BY de.chat_id`,
    });

    // Query weekly_review from agent-service DB
    const weeklyData = await paasClient.post('/api/paas/ops/query', {
      db: 'chiwei',
      sql: `SELECT
              wr.chat_id,
              MAX(wr.week_start) AS latest_week_start,
              COUNT(*) FILTER (WHERE wr.week_start >= CURRENT_DATE - 28) AS review_count_4w
            FROM weekly_review wr
            GROUP BY wr.chat_id`,
    });

    ctx.body = {
      diary: diaryData,
      weekly: weeklyData,
    };
  } catch (err) {
    // If the tables don't exist or query fails, return empty
    console.error('Failed to fetch diary status:', err);
    ctx.body = {
      diary: { columns: [], rows: [] },
      weekly: { columns: [], rows: [] },
    };
  }
});

export default router;
