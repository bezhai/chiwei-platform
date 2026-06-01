import { describe, it, expect, mock, beforeEach } from 'bun:test';

// ---------------------------------------------------------------------------
// 钉死 Task1：Dashboard ops 中转 gateway-rules + reason 强制 + 审计 before/after。
//
// 这一层是"安全入口"：人和 AI 通过 Dashboard 操作 gateway 规则，写操作必须带
// reason、否则拒绝；before/after/snapshot_version 的真值取自 paas-engine 写操作
// 的响应体（disable/enable 的 EnableChange、set-weights 的 WeightsChange），不是
// 中转层自己编。审计 best-effort，handler 把结构化 payload 塞进 c.set('gatewayAudit')，
// 由 audit 中间件合并进 params 顶层固定键。
// ---------------------------------------------------------------------------

// mock paas-client，记录每次转发的 path/body，并返回可控的下游响应
const calls: Array<{ method: string; path: string; body?: unknown; delBody?: unknown }> = [];
let nextResponse: unknown = {};

mock.module('../paas-client', () => {
  const record = (method: string) => async (path: string, bodyOrParams?: unknown) => {
    calls.push({ method, path, body: bodyOrParams });
    return nextResponse;
  };
  // del 的 body 是第 4 个位置参（前 3 个是 path/params/extraHeaders），
  // 单独记录 delBody 以钉死 reason 确实被转发到下游 body 而不是丢在 query。
  const recordDel = async (
    path: string,
    _params?: unknown,
    _extraHeaders?: unknown,
    body?: unknown,
  ) => {
    calls.push({ method: 'DELETE', path, delBody: body });
    return nextResponse;
  };
  return {
    paasClient: {
      get: record('GET'),
      post: record('POST'),
      put: record('PUT'),
      del: recordDel,
    },
    channelClient: {
      get: record('GET'),
      post: record('POST'),
      put: record('PUT'),
      del: recordDel,
    },
  };
});

// 在 mock 之后 import 被测模块
const operationsModule = await import('./operations');
const operationsApp = operationsModule.default;
const { buildGatewayAuditParams } = operationsModule as unknown as {
  buildGatewayAuditParams: (
    action: string,
    ruleName: string | null,
    reason: string | null,
    downstream: unknown,
  ) => Record<string, unknown>;
};

beforeEach(() => {
  calls.length = 0;
  nextResponse = {};
});

describe('gateway-rules 中转：下游路径映射', () => {
  it('GET list → /api/paas/gateway-rules/', async () => {
    nextResponse = [{ name: 'r1' }];
    const res = await operationsApp.request('/api/ops/gateway-rules');
    expect(res.status).toBe(200);
    expect(calls[0]).toMatchObject({ method: 'GET', path: '/api/paas/gateway-rules/' });
  });

  it('GET snapshot → /internal/gateway-rules（version + 规则）', async () => {
    nextResponse = { version: 7, rules: [{ name: 'r1' }] };
    const res = await operationsApp.request('/api/ops/gateway-rules/snapshot');
    expect(res.status).toBe(200);
    expect(calls[0]).toMatchObject({ method: 'GET', path: '/internal/gateway-rules' });
    expect(await res.json()).toMatchObject({ version: 7 });
  });

  it('GET single rule → /api/paas/gateway-rules/{name}', async () => {
    nextResponse = { name: 'r1' };
    await operationsApp.request('/api/ops/gateway-rules/r1');
    expect(calls[0]).toMatchObject({ method: 'GET', path: '/api/paas/gateway-rules/r1' });
  });

  it('POST explain → /api/paas/gateway-rules:explain，只读不要求 reason', async () => {
    nextResponse = { matched: 'r1' };
    const res = await operationsApp.request('/api/ops/gateway-rules:explain', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ path: '/foo', x_lane: 'prod' }),
    });
    expect(res.status).toBe(200);
    expect(calls[0]).toMatchObject({ method: 'POST', path: '/api/paas/gateway-rules:explain' });
  });

  it('PUT upsert → /api/paas/gateway-rules/{name}', async () => {
    nextResponse = { name: 'r1', version: 3 };
    const res = await operationsApp.request('/api/ops/gateway-rules/r1', {
      method: 'PUT',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ reason: 'add canary', path_prefix: '/x' }),
    });
    expect(res.status).toBe(200);
    expect(calls[0]).toMatchObject({ method: 'PUT', path: '/api/paas/gateway-rules/r1' });
  });

  it('DELETE → /api/paas/gateway-rules/{name}', async () => {
    nextResponse = { deleted: 'r1' };
    const res = await operationsApp.request('/api/ops/gateway-rules/r1', {
      method: 'DELETE',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ reason: 'cleanup' }),
    });
    expect(res.status).toBe(200);
    expect(calls[0]).toMatchObject({ method: 'DELETE', path: '/api/paas/gateway-rules/r1' });
  });

  it('DELETE 把 reason 转发到下游 body（不是丢在 query）', async () => {
    nextResponse = { deleted: 'r1' };
    await operationsApp.request('/api/ops/gateway-rules/r1', {
      method: 'DELETE',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ reason: 'cleanup' }),
    });
    expect(calls[0].delBody).toEqual({ reason: 'cleanup' });
  });

  it('disable → /api/paas/gateway-rules/{name}:disable', async () => {
    nextResponse = { name: 'r1', before_enabled: true, after_enabled: false, version: 9 };
    const res = await operationsApp.request('/api/ops/gateway-rules/r1:disable', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ reason: 'stop bleeding' }),
    });
    expect(res.status).toBe(200);
    expect(calls[0]).toMatchObject({ method: 'POST', path: '/api/paas/gateway-rules/r1:disable' });
  });

  it('enable → /api/paas/gateway-rules/{name}:enable', async () => {
    nextResponse = { name: 'r1', before_enabled: false, after_enabled: true, version: 10 };
    const res = await operationsApp.request('/api/ops/gateway-rules/r1:enable', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ reason: 'recovered' }),
    });
    expect(res.status).toBe(200);
    expect(calls[0]).toMatchObject({ method: 'POST', path: '/api/paas/gateway-rules/r1:enable' });
  });

  it('set-weights → /api/paas/gateway-rules/{name}:set-weights', async () => {
    nextResponse = { name: 'r1', before_targets: [], after_targets: [], version: 11 };
    const res = await operationsApp.request('/api/ops/gateway-rules/r1:set-weights', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ reason: 'shift', weights: [{ service: 's', lane: 'prod', weight: 100 }] }),
    });
    expect(res.status).toBe(200);
    expect(calls[0]).toMatchObject({ method: 'POST', path: '/api/paas/gateway-rules/r1:set-weights' });
  });
});

describe('gateway-rules 中转：写操作强制 reason', () => {
  const writeCases: Array<{ name: string; method: string; path: string; body: object }> = [
    { name: 'PUT', method: 'PUT', path: '/api/ops/gateway-rules/r1', body: { path_prefix: '/x' } },
    { name: 'DELETE', method: 'DELETE', path: '/api/ops/gateway-rules/r1', body: {} },
    { name: 'disable', method: 'POST', path: '/api/ops/gateway-rules/r1:disable', body: {} },
    { name: 'enable', method: 'POST', path: '/api/ops/gateway-rules/r1:enable', body: {} },
    { name: 'set-weights', method: 'POST', path: '/api/ops/gateway-rules/r1:set-weights', body: { weights: [] } },
  ];

  for (const tc of writeCases) {
    it(`${tc.name} 缺 reason 直接拒绝 400 且不转发下游`, async () => {
      const res = await operationsApp.request(tc.path, {
        method: tc.method,
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(tc.body),
      });
      expect(res.status).toBe(400);
      expect(calls.length).toBe(0);
    });

    it(`${tc.name} reason 为空字符串也拒绝`, async () => {
      const res = await operationsApp.request(tc.path, {
        method: tc.method,
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ ...tc.body, reason: '   ' }),
      });
      expect(res.status).toBe(400);
      expect(calls.length).toBe(0);
    });
  }

  it('只读 explain 不要求 reason', async () => {
    nextResponse = { matched: 'r1' };
    const res = await operationsApp.request('/api/ops/gateway-rules:explain', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ path: '/foo' }),
    });
    expect(res.status).toBe(200);
  });
});

describe('buildGatewayAuditParams：before/after/snapshot_version 取自下游响应', () => {
  it('disable：从 EnableChange 提取 before/after = enabled 翻转 + version', () => {
    const params = buildGatewayAuditParams('disable', 'r1', 'stop bleeding', {
      name: 'r1',
      before_enabled: true,
      after_enabled: false,
      version: 9,
    });
    expect(params).toEqual({
      rule_name: 'r1',
      reason: 'stop bleeding',
      before: { enabled: true },
      after: { enabled: false },
      snapshot_version: 9,
    });
  });

  it('set-weights：从 WeightsChange 提取 before/after = targets + version', () => {
    const before = [{ service: 's', lane: 'prod', weight: 90 }];
    const after = [{ service: 's', lane: 'prod', weight: 0 }];
    const params = buildGatewayAuditParams('set-weights', 'r1', 'shift', {
      name: 'r1',
      before_targets: before,
      after_targets: after,
      version: 11,
    });
    expect(params).toEqual({
      rule_name: 'r1',
      reason: 'shift',
      before: { targets: before },
      after: { targets: after },
      snapshot_version: 11,
    });
  });

  it('upsert：snapshot_version 取下游的快照版本，不是规则自己的 version', () => {
    // 下游响应平铺规则字段（含 rule version=3）+ 事务分配的 snapshot_version=42。
    // 审计游标必须是 snapshot_version，绝不能误取 rule version。
    const downstream = { name: 'r1', version: 3, path_prefix: '/x', snapshot_version: 42 };
    const params = buildGatewayAuditParams('update', 'r1', 'add canary', downstream);
    expect(params.rule_name).toBe('r1');
    expect(params.reason).toBe('add canary');
    expect(params.snapshot_version).toBe(42);
    expect(params.after).toEqual(downstream);
  });

  it('delete：snapshot_version 取下游事务分配的快照版本（不再恒为 null）', () => {
    const params = buildGatewayAuditParams('delete', 'r1', 'cleanup', {
      deleted: 'r1',
      snapshot_version: 43,
    });
    expect(params.rule_name).toBe('r1');
    expect(params.snapshot_version).toBe(43);
  });

  it('固定顶层键：永远含 rule_name/reason/before/after/snapshot_version 五键', () => {
    const params = buildGatewayAuditParams('delete', 'r1', 'cleanup', { deleted: 'r1' });
    expect(Object.keys(params).sort()).toEqual(
      ['after', 'before', 'reason', 'rule_name', 'snapshot_version'].sort(),
    );
  });
});

describe('Task3：快照历史列表 + 一键回滚中转', () => {
  it('GET snapshots → /api/paas/gateway-rules/snapshots（只读，不要求 reason，透传 limit）', async () => {
    nextResponse = [{ snapshot_version: 9, rules: [] }];
    const res = await operationsApp.request('/api/ops/gateway-rules/snapshots?limit=5');
    expect(res.status).toBe(200);
    expect(calls[0]).toMatchObject({ method: 'GET', path: '/api/paas/gateway-rules/snapshots?limit=5' });
  });

  it('GET snapshots 无 limit → 不带查询串', async () => {
    nextResponse = [];
    await operationsApp.request('/api/ops/gateway-rules/snapshots');
    expect(calls[0]).toMatchObject({ method: 'GET', path: '/api/paas/gateway-rules/snapshots' });
  });

  it('POST rollback → /api/paas/gateway-rules:rollback，转发 body', async () => {
    nextResponse = { snapshot_version: 12, reason: 'recover', created_by: 'ops' };
    const res = await operationsApp.request('/api/ops/gateway-rules:rollback', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ snapshot_version: 8, reason: 'recover' }),
    });
    expect(res.status).toBe(200);
    expect(calls[0]).toMatchObject({ method: 'POST', path: '/api/paas/gateway-rules:rollback' });
    expect(calls[0].body).toMatchObject({ snapshot_version: 8, reason: 'recover' });
  });

  it('rollback 缺 reason 直接拒绝 400 且不转发下游', async () => {
    const res = await operationsApp.request('/api/ops/gateway-rules:rollback', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ snapshot_version: 8 }),
    });
    expect(res.status).toBe(400);
    expect(calls.length).toBe(0);
  });

  it('rollback reason 全空白也拒绝', async () => {
    const res = await operationsApp.request('/api/ops/gateway-rules:rollback', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ snapshot_version: 8, reason: '   ' }),
    });
    expect(res.status).toBe(400);
    expect(calls.length).toBe(0);
  });

  it('rollback 把 snapshot_version + reason 塞进审计 gatewayAudit', async () => {
    nextResponse = { snapshot_version: 12, reason: 'recover', created_by: 'ops' };
    let captured: unknown = null;
    const { Hono } = await import('hono');
    const probe = new Hono();
    probe.use('*', async (c, next) => {
      await next();
      captured = c.get('gatewayAudit' as never);
    });
    probe.route('/', operationsApp);
    await probe.request('/api/ops/gateway-rules:rollback', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ snapshot_version: 8, reason: 'recover' }),
    });
    expect(captured).toMatchObject({
      reason: 'recover',
      snapshot_version: 12,
    });
  });
});

describe('audit 中间件合并 gatewayAudit 进 params 顶层', () => {
  it('handler 写操作把固定键 stash 到 c.set，中间件取出来落 params', async () => {
    // 直接验证：disable handler 转发后，把下游 before/after 塞进了 context。
    // 用一个 Hono app + 探针中间件读 c.get('gatewayAudit')。
    nextResponse = { name: 'r1', before_enabled: true, after_enabled: false, version: 12 };
    let captured: unknown = null;
    const { Hono } = await import('hono');
    const probe = new Hono();
    probe.use('*', async (c, next) => {
      await next();
      captured = c.get('gatewayAudit' as never);
    });
    probe.route('/', operationsApp);
    await probe.request('/api/ops/gateway-rules/r1:disable', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ reason: 'stop' }),
    });
    expect(captured).toEqual({
      rule_name: 'r1',
      reason: 'stop',
      before: { enabled: true },
      after: { enabled: false },
      snapshot_version: 12,
    });
  });
});
