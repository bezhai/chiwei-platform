import { Hono } from 'hono';
import { paasClient, larkClient } from '../paas-client';

const app = new Hono();

// ---------- 读操作 ----------

/** GET /api/ops/services — 全部服务 + Release 状态 */
app.get('/api/ops/services', async (c) => {
  const [apps, releases] = await Promise.all([
    paasClient.get('/api/paas/apps/'),
    paasClient.get('/api/paas/releases/'),
  ]);
  return c.json({ apps, releases });
});

/** GET /api/ops/services/:app/pods — 指定服务的 Pod 状态 */
app.get('/api/ops/services/:app/pods', async (c) => {
  const appName = c.req.param('app');
  const lane = c.req.query('lane') || 'prod';

  // Step 1: find release ID
  const releases = (await paasClient.get('/api/paas/releases/', { app: appName, lane })) as Array<{ id: string }>;
  if (!Array.isArray(releases) || releases.length === 0) {
    return c.json({ message: `No release found for ${appName} in lane ${lane}` }, 404);
  }

  // Step 2: get pod status
  const status = await paasClient.get(`/api/paas/releases/${releases[0].id}/status`);
  return c.json(status);
});

/** GET /api/ops/builds/:app/latest — 最近成功构建 */
app.get('/api/ops/builds/:app/latest', async (c) => {
  const appName = c.req.param('app');
  const data = await paasClient.get(`/api/paas/apps/${appName}/builds/latest`);
  return c.json(data);
});

/** POST /api/ops/db-query — 只读 SQL 查询 */
app.post('/api/ops/db-query', async (c) => {
  const { sql, db } = (await c.req.json()) as { sql?: string; db?: string };
  if (!sql) {
    return c.json({ message: 'sql is required' }, 400);
  }

  // Basic safety: block write operations
  const normalized = sql.trim().toUpperCase();
  const forbidden = ['INSERT', 'UPDATE', 'DELETE', 'DROP', 'ALTER', 'TRUNCATE', 'CREATE', 'GRANT', 'REVOKE'];
  if (forbidden.some((kw) => normalized.startsWith(kw))) {
    return c.json({ message: 'Only SELECT queries are allowed' }, 403);
  }

  const data = await paasClient.post('/api/paas/ops/query', {
    sql,
    db: db || 'paas_engine',
  });
  return c.json(data);
});

/** GET /api/ops/lane-bindings — 列出泳道绑定 */
app.get('/api/ops/lane-bindings', async (c) => {
  const data = await larkClient.get('/api/lark/lane-bindings');
  return c.json(data);
});

// ---------- DDL/DML 变更审批 ----------

/** 从请求中提取需要转发的 x-lane header */
function laneHeaders(c: { req: { header: (name: string) => string | undefined } }): Record<string, string> | undefined {
  const lane = c.req.header('x-lane');
  return lane ? { 'x-lane': lane } : undefined;
}

/** POST /api/ops/db-mutations — 提交 DDL/DML 变更申请 */
app.post('/api/ops/db-mutations', async (c) => {
  const data = await paasClient.post('/api/paas/ops/mutations', await c.req.json(), laneHeaders(c));
  return c.json(data);
});

/** GET /api/ops/db-mutations — 列出变更申请（可选 ?status=pending） */
app.get('/api/ops/db-mutations', async (c) => {
  const params: Record<string, string> = {};
  const status = c.req.query('status');
  if (status) params.status = status;
  const data = await paasClient.get('/api/paas/ops/mutations', params, laneHeaders(c));
  return c.json(data);
});

/** GET /api/ops/db-mutations/:id — 查看单条变更详情 */
app.get('/api/ops/db-mutations/:id', async (c) => {
  const data = await paasClient.get(`/api/paas/ops/mutations/${c.req.param('id')}`, undefined, laneHeaders(c));
  return c.json(data);
});

/** POST /api/ops/db-mutations/:id/approve — 审批通过并执行 */
app.post('/api/ops/db-mutations/:id/approve', async (c) => {
  const data = await paasClient.post(`/api/paas/ops/mutations/${c.req.param('id')}/approve`, await c.req.json(), laneHeaders(c));
  return c.json(data);
});

/** POST /api/ops/db-mutations/:id/reject — 拒绝变更 */
app.post('/api/ops/db-mutations/:id/reject', async (c) => {
  const data = await paasClient.post(`/api/paas/ops/mutations/${c.req.param('id')}/reject`, await c.req.json(), laneHeaders(c));
  return c.json(data);
});

// ---------- 写操作 ----------

/** POST /api/ops/lane-bindings — 绑定泳道 */
app.post('/api/ops/lane-bindings', async (c) => {
  const { route_type, route_key, lane_name } = (await c.req.json()) as {
    route_type?: string;
    route_key?: string;
    lane_name?: string;
  };
  if (!route_type || !route_key || !lane_name) {
    return c.json({ message: 'route_type, route_key, and lane_name are required' }, 400);
  }
  const data = await larkClient.post('/api/lark/lane-bindings', {
    route_type,
    route_key,
    lane_name,
  });
  return c.json(data);
});

/** DELETE /api/ops/lane-bindings — 解绑泳道 */
app.delete('/api/ops/lane-bindings', async (c) => {
  const type = c.req.query('type');
  const key = c.req.query('key');
  if (!type || !key) {
    return c.json({ message: 'type and key query params are required' }, 400);
  }
  const data = await larkClient.del('/api/lark/lane-bindings', { type, key });
  return c.json(data);
});

/** POST /api/ops/trigger-diary — 触发日记生成 */
app.post('/api/ops/trigger-diary', async (c) => {
  const { chat_id, target_date } = (await c.req.json()) as {
    chat_id?: string;
    target_date?: string;
  };
  if (!chat_id) {
    return c.json({ message: 'chat_id is required' }, 400);
  }
  const params: Record<string, string> = { chat_id };
  if (target_date) params.target_date = target_date;

  const data = await paasClient.post(
    `/api/agent/admin/trigger-diary?${new URLSearchParams(params).toString()}`,
  );
  return c.json(data);
});

/** POST /api/ops/trigger-weekly-review — 触发周记生成 */
app.post('/api/ops/trigger-weekly-review', async (c) => {
  const { chat_id, week_start } = (await c.req.json()) as {
    chat_id?: string;
    week_start?: string;
  };
  if (!chat_id) {
    return c.json({ message: 'chat_id is required' }, 400);
  }
  const params: Record<string, string> = { chat_id };
  if (week_start) params.week_start = week_start;

  const data = await paasClient.post(
    `/api/agent/admin/trigger-weekly-review?${new URLSearchParams(params).toString()}`,
  );
  return c.json(data);
});

export default app;
