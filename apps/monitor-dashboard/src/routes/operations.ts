import Router from '@koa/router';
import { paasClient, larkClient } from '../paas-client';

const router = new Router();

// ---------- 读操作 ----------

/** GET /api/ops/services — 全部服务 + Release 状态 */
router.get('/api/ops/services', async (ctx) => {
  const [apps, releases] = await Promise.all([
    paasClient.get('/api/paas/apps/'),
    paasClient.get('/api/paas/releases/'),
  ]);
  ctx.body = { apps, releases };
});

/** GET /api/ops/services/:app/pods — 指定服务的 Pod 状态 */
router.get('/api/ops/services/:app/pods', async (ctx) => {
  const { app } = ctx.params;
  const lane = (ctx.query.lane as string) || 'prod';

  // Step 1: find release ID
  const releases = (await paasClient.get('/api/paas/releases/', { app, lane })) as Array<{ id: string }>;
  if (!Array.isArray(releases) || releases.length === 0) {
    ctx.status = 404;
    ctx.body = { message: `No release found for ${app} in lane ${lane}` };
    return;
  }

  // Step 2: get pod status
  const status = await paasClient.get(`/api/paas/releases/${releases[0].id}/status`);
  ctx.body = status;
});

/** GET /api/ops/builds/:app/latest — 最近成功构建 */
router.get('/api/ops/builds/:app/latest', async (ctx) => {
  const { app } = ctx.params;
  const data = await paasClient.get(`/api/paas/apps/${app}/builds/latest`);
  ctx.body = data;
});

/** POST /api/ops/db-query — 只读 SQL 查询 */
router.post('/api/ops/db-query', async (ctx) => {
  const { sql, db } = ctx.request.body as { sql?: string; db?: string };
  if (!sql) {
    ctx.status = 400;
    ctx.body = { message: 'sql is required' };
    return;
  }

  // Basic safety: block write operations
  const normalized = sql.trim().toUpperCase();
  const forbidden = ['INSERT', 'UPDATE', 'DELETE', 'DROP', 'ALTER', 'TRUNCATE', 'CREATE', 'GRANT', 'REVOKE'];
  if (forbidden.some((kw) => normalized.startsWith(kw))) {
    ctx.status = 403;
    ctx.body = { message: 'Only SELECT queries are allowed' };
    return;
  }

  const data = await paasClient.post('/api/paas/ops/query', {
    sql,
    db: db || 'paas_engine',
  });
  ctx.body = data;
});

/** GET /api/ops/lane-bindings — 列出泳道绑定 */
router.get('/api/ops/lane-bindings', async (ctx) => {
  const data = await larkClient.get('/api/lark/lane-bindings');
  ctx.body = data;
});

// ---------- DDL/DML 变更审批 ----------

/** 从请求中提取需要转发的 x-lane header */
function laneHeaders(ctx: { headers: Record<string, string | string[] | undefined> }): Record<string, string> | undefined {
  const lane = ctx.headers['x-lane'];
  return lane ? { 'x-lane': String(lane) } : undefined;
}

/** POST /api/ops/db-mutations — 提交 DDL/DML 变更申请 */
router.post('/api/ops/db-mutations', async (ctx) => {
  const data = await paasClient.post('/api/paas/ops/mutations', ctx.request.body, laneHeaders(ctx));
  ctx.body = data;
});

/** GET /api/ops/db-mutations — 列出变更申请（可选 ?status=pending） */
router.get('/api/ops/db-mutations', async (ctx) => {
  const params: Record<string, string> = {};
  if (ctx.query.status) params.status = ctx.query.status as string;
  const data = await paasClient.get('/api/paas/ops/mutations', params, laneHeaders(ctx));
  ctx.body = data;
});

/** GET /api/ops/db-mutations/:id — 查看单条变更详情 */
router.get('/api/ops/db-mutations/:id', async (ctx) => {
  const data = await paasClient.get(`/api/paas/ops/mutations/${ctx.params.id}`, undefined, laneHeaders(ctx));
  ctx.body = data;
});

/** POST /api/ops/db-mutations/:id/approve — 审批通过并执行 */
router.post('/api/ops/db-mutations/:id/approve', async (ctx) => {
  const data = await paasClient.post(`/api/paas/ops/mutations/${ctx.params.id}/approve`, ctx.request.body, laneHeaders(ctx));
  ctx.body = data;
});

/** POST /api/ops/db-mutations/:id/reject — 拒绝变更 */
router.post('/api/ops/db-mutations/:id/reject', async (ctx) => {
  const data = await paasClient.post(`/api/paas/ops/mutations/${ctx.params.id}/reject`, ctx.request.body, laneHeaders(ctx));
  ctx.body = data;
});

// ---------- 写操作 ----------

/** POST /api/ops/lane-bindings — 绑定泳道 */
router.post('/api/ops/lane-bindings', async (ctx) => {
  const { route_type, route_key, lane_name } = ctx.request.body as {
    route_type?: string;
    route_key?: string;
    lane_name?: string;
  };
  if (!route_type || !route_key || !lane_name) {
    ctx.status = 400;
    ctx.body = { message: 'route_type, route_key, and lane_name are required' };
    return;
  }
  const data = await larkClient.post('/api/lark/lane-bindings', {
    route_type,
    route_key,
    lane_name,
  });
  ctx.body = data;
});

/** DELETE /api/ops/lane-bindings — 解绑泳道 */
router.delete('/api/ops/lane-bindings', async (ctx) => {
  const type = ctx.query.type as string;
  const key = ctx.query.key as string;
  if (!type || !key) {
    ctx.status = 400;
    ctx.body = { message: 'type and key query params are required' };
    return;
  }
  const data = await larkClient.del('/api/lark/lane-bindings', { type, key });
  ctx.body = data;
});

/** POST /api/ops/trigger-diary — 触发日记生成 */
router.post('/api/ops/trigger-diary', async (ctx) => {
  const { chat_id, target_date } = ctx.request.body as {
    chat_id?: string;
    target_date?: string;
  };
  if (!chat_id) {
    ctx.status = 400;
    ctx.body = { message: 'chat_id is required' };
    return;
  }
  const params: Record<string, string> = { chat_id };
  if (target_date) params.target_date = target_date;

  const data = await paasClient.post(
    `/api/agent/admin/trigger-diary?${new URLSearchParams(params).toString()}`,
  );
  ctx.body = data;
});

/** POST /api/ops/trigger-weekly-review — 触发周记生成 */
router.post('/api/ops/trigger-weekly-review', async (ctx) => {
  const { chat_id, week_start } = ctx.request.body as {
    chat_id?: string;
    week_start?: string;
  };
  if (!chat_id) {
    ctx.status = 400;
    ctx.body = { message: 'chat_id is required' };
    return;
  }
  const params: Record<string, string> = { chat_id };
  if (week_start) params.week_start = week_start;

  const data = await paasClient.post(
    `/api/agent/admin/trigger-weekly-review?${new URLSearchParams(params).toString()}`,
  );
  ctx.body = data;
});

export default router;
