import { describe, it, expect, mock } from 'bun:test';

// mock DB：捕获 auditMiddleware 落库的对象，验合并逻辑真把 gatewayAudit 提到 params 顶层。
const savedRows: Array<Record<string, unknown>> = [];
mock.module('../db', () => ({
  AppDataSource: {
    getRepository: () => ({
      save: async (row: Record<string, unknown>) => {
        savedRows.push(row);
        return row;
      },
    }),
  },
}));

import { deriveAction, auditMiddleware } from './audit';

// 钉死 gateway-rules 各路径的 action 映射（list/get/explain/create-update/delete/
// disable/enable/set-weights），审计要能按 action 检索到这些操作。

describe('deriveAction：gateway-rules 路径映射', () => {
  const cases: Array<[string, string, string]> = [
    ['GET', '/dashboard/api/ops/gateway-rules', 'ops.gateway-rules.list'],
    ['GET', '/dashboard/api/ops/gateway-rules/snapshot', 'ops.gateway-rules.snapshot'],
    ['GET', '/dashboard/api/ops/gateway-rules/r1', 'ops.gateway-rules.get'],
    ['POST', '/dashboard/api/ops/gateway-rules:explain', 'ops.gateway-rules.explain'],
    ['PUT', '/dashboard/api/ops/gateway-rules/r1', 'ops.gateway-rules.update'],
    ['DELETE', '/dashboard/api/ops/gateway-rules/r1', 'ops.gateway-rules.delete'],
    ['POST', '/dashboard/api/ops/gateway-rules/r1:disable', 'ops.gateway-rules.disable'],
    ['POST', '/dashboard/api/ops/gateway-rules/r1:enable', 'ops.gateway-rules.enable'],
    ['POST', '/dashboard/api/ops/gateway-rules/r1:set-weights', 'ops.gateway-rules.set-weights'],
    ['GET', '/dashboard/api/ops/gateway-rules/snapshots', 'ops.gateway-rules.snapshots'],
    ['POST', '/dashboard/api/ops/gateway-rules:rollback', 'ops.gateway-rules.rollback'],
  ];

  for (const [method, path, expected] of cases) {
    it(`${method} ${path} → ${expected}`, () => {
      expect(deriveAction(method, path)).toBe(expected);
    });
  }

  it('snapshot 不被误判成 get（{name} 不能吃掉 snapshot）', () => {
    expect(deriveAction('GET', '/dashboard/api/ops/gateway-rules/snapshot')).not.toBe('ops.gateway-rules.get');
  });

  it('snapshots（历史列表）不被误判成 get', () => {
    expect(deriveAction('GET', '/dashboard/api/ops/gateway-rules/snapshots')).not.toBe('ops.gateway-rules.get');
  });
});

// 钉死：handler 通过 c.set('gatewayAudit') stash 的固定五键，必须被 auditMiddleware
// 提升到落库 params 的顶层（不是嵌在 params.gatewayAudit 里），否则 audit_logs.params
// 没法按 rule_name / snapshot_version 做 JSONB 检索。
describe('auditMiddleware：gatewayAudit 五键提升到落库 params 顶层', () => {
  it('disable 写操作的 before/after/snapshot_version 进 params 顶层', async () => {
    savedRows.length = 0;
    const { Hono } = await import('hono');
    const app = new Hono();
    app.use('*', auditMiddleware);
    app.post('/dashboard/api/ops/gateway-rules/r1:disable', (c) => {
      c.set('caller' as never, 'tester' as never);
      c.set('gatewayAudit' as never, {
        rule_name: 'r1',
        reason: 'stop',
        before: { enabled: true },
        after: { enabled: false },
        snapshot_version: 12,
      } as never);
      return c.json({ ok: true });
    });

    const res = await app.request('/dashboard/api/ops/gateway-rules/r1:disable', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ reason: 'stop' }),
    });
    expect(res.status).toBe(200);

    expect(savedRows.length).toBe(1);
    const row = savedRows[0];
    expect(row.action).toBe('ops.gateway-rules.disable');
    const params = row.params as Record<string, unknown>;
    expect(params.rule_name).toBe('r1');
    expect(params.snapshot_version).toBe(12);
    expect(params.before).toEqual({ enabled: true });
    expect(params.after).toEqual({ enabled: false });
  });
});
