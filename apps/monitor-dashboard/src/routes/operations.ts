import { Hono } from 'hono';
import type { AppEnv } from '../types';
import { channelClient, paasClient } from '../paas-client';

const app = new Hono<AppEnv>();

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
  const data = await channelClient.get('/api/lane-bindings');
  return c.json(data);
});

// ---------- DDL/DML 变更审批 ----------

/** 从请求中提取需要转发的泳道 header */
function laneHeaders(c: { req: { header: (name: string) => string | undefined } }): Record<string, string> | undefined {
  const lane = c.req.header('x-ctx-lane') || c.req.header('x-lane');
  return lane ? { 'x-ctx-lane': lane } : undefined;
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
  const data = await channelClient.post('/api/lane-bindings', {
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
  const data = await channelClient.del('/api/lane-bindings', { type, key });
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

// ---------- gateway-rules 中转（流量调度安全入口） ----------
//
// 这一层是"安全入口"：人和 AI 通过 Dashboard 安全地列规则 / 看当前快照 / explain /
// 增删改 / 止血。转发到 paas-engine 的 gateway-rules API（X-API-Key 由 paasClient 注入）。
// 写操作（PUT/DELETE/disable/enable/set-weights）强制带 reason、缺失直接拒绝 400；
// before/after/snapshot_version 的真值取自 paas-engine 写操作的响应体，回填进审计
// （c.set('gatewayAudit')，由 audit 中间件落 audit_logs.params 顶层固定键）。

/** 写操作 reason 必填：缺失或全空白返回非空错误信息，校验通过返回 null。 */
function requireReason(reason: unknown): string | null {
  if (typeof reason !== 'string' || reason.trim() === '') {
    return 'reason is required for gateway-rules write operations';
  }
  return null;
}

/**
 * 从 paas-engine 写操作响应提取审计 before/after/snapshot_version，固定五个顶层键。
 * - disable/enable：下游 EnableChange {before_enabled, after_enabled, version}
 * - set-weights：下游 WeightsChange {before_targets, after_targets, version}
 * - upsert(update)：下游回结果规则 {..., version}，after = 规则本身、before 未知
 * - delete：下游回 {deleted}，无 before/after/version
 * before/after 都是下游响应的真值，不是中转层猜的。
 */
export function buildGatewayAuditParams(
  action: string,
  ruleName: string | null,
  reason: string | null,
  downstream: unknown,
): Record<string, unknown> {
  const d = (downstream && typeof downstream === 'object' ? downstream : {}) as Record<string, unknown>;
  let before: unknown = null;
  let after: unknown = null;
  let snapshotVersion: unknown = null;

  if (action === 'disable' || action === 'enable') {
    before = { enabled: d.before_enabled ?? null };
    after = { enabled: d.after_enabled ?? null };
    snapshotVersion = d.version ?? null;
  } else if (action === 'set-weights') {
    before = { targets: d.before_targets ?? null };
    after = { targets: d.after_targets ?? null };
    snapshotVersion = d.version ?? null;
  } else if (action === 'update') {
    // upsert 下游返回写入后的规则本身（含 rule version）+ 事务分配的 snapshot_version。
    // 审计游标取 snapshot_version，不是规则自己的 version（两者语义不同，不能混）。
    after = downstream ?? null;
    snapshotVersion = d.snapshot_version ?? null;
  } else if (action === 'delete') {
    // delete 下游回 {deleted, snapshot_version}，snapshot_version 是事务分配的快照版本。
    after = downstream ?? null;
    snapshotVersion = d.snapshot_version ?? null;
  } else if (action === 'rollback') {
    // rollback 下游回新生成的快照 {snapshot_version, reason, created_by}；
    // after = 这条新快照、snapshot_version = 回滚后分配的更大新版本。
    after = downstream ?? null;
    snapshotVersion = d.snapshot_version ?? null;
  }

  return {
    rule_name: ruleName,
    reason: reason ?? null,
    before,
    after,
    snapshot_version: snapshotVersion,
  };
}

/** GET /api/ops/gateway-rules — 列出全部规则（只读，不要求 reason） */
app.get('/api/ops/gateway-rules', async (c) => {
  const data = await paasClient.get('/api/paas/gateway-rules/');
  return c.json(data);
});

/** GET /api/ops/gateway-rules/snapshot — 看 paas-engine 当前下发的期望配置（version + 规则） */
app.get('/api/ops/gateway-rules/snapshot', async (c) => {
  const data = await paasClient.get('/internal/gateway-rules');
  return c.json(data);
});

/** GET /api/ops/gateway-rules/snapshots — 列出最近 N 条规则快照历史（只读，透传 limit） */
app.get('/api/ops/gateway-rules/snapshots', async (c) => {
  const limit = c.req.query('limit');
  const path = limit ? `/api/paas/gateway-rules/snapshots?limit=${encodeURIComponent(limit)}` : '/api/paas/gateway-rules/snapshots';
  const data = await paasClient.get(path);
  return c.json(data);
});

/** GET /api/ops/gateway-rules/:name — 看单条规则（只读） */
app.get('/api/ops/gateway-rules/:name', async (c) => {
  const data = await paasClient.get(`/api/paas/gateway-rules/${c.req.param('name')}`);
  return c.json(data);
});

/** POST /api/ops/gateway-rules:explain — 预览命中（只读，不要求 reason） */
app.post('/api/ops/gateway-rules:explain', async (c) => {
  const data = await paasClient.post('/api/paas/gateway-rules:explain', await c.req.json());
  return c.json(data);
});

/** POST /api/ops/gateway-rules:rollback — 回滚到历史某版本（写，强制 reason，落审计） */
app.post('/api/ops/gateway-rules:rollback', async (c) => {
  const body = (await c.req.json().catch(() => ({}))) as Record<string, unknown>;
  const err = requireReason(body.reason);
  if (err) return c.json({ message: err }, 400);

  const data = await paasClient.post('/api/paas/gateway-rules:rollback', body);
  c.set('gatewayAudit', buildGatewayAuditParams('rollback', null, body.reason as string, data));
  return c.json(data);
});

/** PUT /api/ops/gateway-rules/:name — 创建/更新规则（写，强制 reason） */
app.put('/api/ops/gateway-rules/:name', async (c) => {
  const name = c.req.param('name');
  const body = (await c.req.json()) as Record<string, unknown>;
  const err = requireReason(body.reason);
  if (err) return c.json({ message: err }, 400);

  const data = await paasClient.put(`/api/paas/gateway-rules/${name}`, body);
  c.set('gatewayAudit', buildGatewayAuditParams('update', name, body.reason as string, data));
  return c.json(data);
});

/** DELETE /api/ops/gateway-rules/:name — 删除规则（写，强制 reason） */
app.delete('/api/ops/gateway-rules/:name', async (c) => {
  const name = c.req.param('name');
  const body = (await c.req.json().catch(() => ({}))) as Record<string, unknown>;
  const err = requireReason(body.reason);
  if (err) return c.json({ message: err }, 400);

  const data = await paasClient.del(`/api/paas/gateway-rules/${name}`, undefined, undefined, { reason: body.reason });
  c.set('gatewayAudit', buildGatewayAuditParams('delete', name, body.reason as string, data));
  return c.json(data);
});

/** POST /api/ops/gateway-rules/:name:disable — 止血禁用（写，强制 reason） */
app.post('/api/ops/gateway-rules/:nameAction{[^/]+:disable}', async (c) => {
  const name = c.req.param('nameAction').replace(/:disable$/, '');
  const body = (await c.req.json()) as Record<string, unknown>;
  const err = requireReason(body.reason);
  if (err) return c.json({ message: err }, 400);

  const data = await paasClient.post(`/api/paas/gateway-rules/${name}:disable`, body);
  c.set('gatewayAudit', buildGatewayAuditParams('disable', name, body.reason as string, data));
  return c.json(data);
});

/** POST /api/ops/gateway-rules/:name:enable — 恢复启用（写，强制 reason） */
app.post('/api/ops/gateway-rules/:nameAction{[^/]+:enable}', async (c) => {
  const name = c.req.param('nameAction').replace(/:enable$/, '');
  const body = (await c.req.json()) as Record<string, unknown>;
  const err = requireReason(body.reason);
  if (err) return c.json({ message: err }, 400);

  const data = await paasClient.post(`/api/paas/gateway-rules/${name}:enable`, body);
  c.set('gatewayAudit', buildGatewayAuditParams('enable', name, body.reason as string, data));
  return c.json(data);
});

/** POST /api/ops/gateway-rules/:name:set-weights — 整体调权（写，强制 reason） */
app.post('/api/ops/gateway-rules/:nameAction{[^/]+:set-weights}', async (c) => {
  const name = c.req.param('nameAction').replace(/:set-weights$/, '');
  const body = (await c.req.json()) as Record<string, unknown>;
  const err = requireReason(body.reason);
  if (err) return c.json({ message: err }, 400);

  const data = await paasClient.post(`/api/paas/gateway-rules/${name}:set-weights`, body);
  c.set('gatewayAudit', buildGatewayAuditParams('set-weights', name, body.reason as string, data));
  return c.json(data);
});

export default app;
