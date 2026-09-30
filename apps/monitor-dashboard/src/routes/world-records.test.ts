import { describe, it, expect, beforeEach } from 'bun:test';
import { Hono } from 'hono';

// ---------------------------------------------------------------------------
// Dashboard 转发 world 的记录人工读写接口（/admin/world/records*）。
//
// world 是另一个服务（同一个镜像上的另一个 App），开发机够不到它的内网凭据，只能从这里
// 过：这一侧认 PAAS_TOKEN、落审计，带着 INNER_HTTP_SECRET、泳道头和调用者转过去。钉死的：
// 1. 路径、参数、请求体原样映射（校验是上游的事）。
// 2. 泳道走 header：x-lane / x-ctx-lane 归一成 x-ctx-lane；不走 query。
// 3. 写和删把调用者作为 X-Operator 带下去，上游按它记日志。
// 4. 上游自报的 lane 原样透传；上游拒绝时状态码和 lane 一起带回来。
// 5. 审计里记下请求泳道、执行泳道、哪一份、改之前和改之后的指纹、结果。
// ---------------------------------------------------------------------------

import { createWorldRecordRoutes, type RecordAuditStore, type WorldRecordsClient } from './world-records';

type Call = { method: string; path: string; params?: unknown; body?: unknown; extraHeaders?: unknown };

const calls: Call[] = [];
let nextResult: { ok: unknown } | { fail: unknown } = { ok: {} };

function settle(): Promise<unknown> {
  if ('fail' in nextResult) return Promise.reject(nextResult.fail);
  return Promise.resolve(nextResult.ok);
}

const stubClient: WorldRecordsClient = {
  get(path, params, extraHeaders) {
    calls.push({ method: 'GET', path, params, extraHeaders });
    return settle();
  },
  put(path, body, extraHeaders) {
    calls.push({ method: 'PUT', path, body, extraHeaders });
    return settle();
  },
  del(path, params, extraHeaders) {
    calls.push({ method: 'DELETE', path, params, extraHeaders });
    return settle();
  },
};

function upstreamError(status: number, detail: unknown): Error {
  const err = new Error(`Request failed with status code ${status}`) as Error & { response?: unknown };
  err.response = { status, data: { detail } };
  return err;
}

let lastStash: Record<string, unknown> | undefined;
let lastAuditWritten: unknown;

// 审计落库的替身：写和删先落一条（begin），转发之后补记结果（finish）。
type AuditRow = { id: number; caller: string; action: string; params: Record<string, unknown>; result: string };
const auditRows: AuditRow[] = [];
const auditFinishes: Array<{ id: number; patch: Record<string, unknown> }> = [];
let auditBeginFails = false;
let auditFinishFails = false;
const stubAudit: RecordAuditStore = {
  async begin(row) {
    if (auditBeginFails) throw new Error('audit db down');
    const id = auditRows.length + 1;
    auditRows.push({ id, ...row, result: 'pending' });
    return id;
  },
  async finish(id, patch) {
    if (auditFinishFails) throw new Error('audit db down');
    auditFinishes.push({ id, patch: patch as Record<string, unknown> });
  },
};

async function call(
  path: string,
  init?: { method?: string; headers?: Record<string, string>; body?: unknown },
): Promise<{ status: number; body: Record<string, unknown> }> {
  const app = new Hono();
  app.use('*', async (c, next) => {
    c.set('caller' as never, 'claude-code' as never);
    await next();
    lastStash = c.get('gatewayAudit' as never) as Record<string, unknown> | undefined;
    lastAuditWritten = c.get('auditWritten' as never);
  });
  app.route('/', createWorldRecordRoutes(stubClient, stubAudit));
  const req: RequestInit = { method: init?.method || 'GET', headers: init?.headers };
  if (init?.body !== undefined) {
    req.headers = { 'content-type': 'application/json', ...(init.headers || {}) };
    req.body = typeof init.body === 'string' ? init.body : JSON.stringify(init.body);
  }
  const res = await app.request(path, req);
  let body: Record<string, unknown> = {};
  try {
    body = (await res.json()) as Record<string, unknown>;
  } catch {
    /* empty body */
  }
  return { status: res.status, body };
}

beforeEach(() => {
  calls.length = 0;
  nextResult = { ok: {} };
  lastStash = undefined;
  lastAuditWritten = undefined;
  auditRows.length = 0;
  auditFinishes.length = 0;
  auditBeginFails = false;
  auditFinishFails = false;
});

describe('路径、参数、请求体', () => {
  it('GET records → GET /admin/world/records，带泳道头', async () => {
    nextResult = { ok: { lane: 'coe-world', records: [] } };
    const res = await call('/api/ops/world/records', { headers: { 'x-lane': 'coe-world' } });
    expect(res.status).toBe(200);
    expect(res.body).toEqual({ lane: 'coe-world', records: [] });
    expect(calls).toEqual([
      { method: 'GET', path: '/admin/world/records', params: {}, extraHeaders: { 'x-ctx-lane': 'coe-world' } },
    ]);
  });

  it('GET document → GET /admin/world/records/document，只下发 path', async () => {
    nextResult = { ok: { lane: 'coe-world', path: '地方/甲.md', text: '一。', fingerprint: 'f1', updated_at: 't' } };
    await call('/api/ops/world/records/document?path=%E5%9C%B0%E6%96%B9%2F%E7%94%B2.md&x=1', {
      headers: { 'x-ctx-lane': 'coe-world' },
    });
    expect(calls).toEqual([
      {
        method: 'GET',
        path: '/admin/world/records/document',
        params: { path: '地方/甲.md' },
        extraHeaders: { 'x-ctx-lane': 'coe-world' },
      },
    ]);
  });

  it('PUT document → PUT /admin/world/records/document，请求体原样交下去，调用者作为 X-Operator', async () => {
    nextResult = {
      ok: { lane: 'coe-world', path: '地方/甲.md', fingerprint: 'f2', previous_fingerprint: 'f1', created: false },
    };
    const body = { path: '地方/甲.md', text: '二。', fingerprint: 'f1' };
    const res = await call('/api/ops/world/records/document', {
      method: 'PUT',
      headers: { 'x-lane': 'coe-world' },
      body,
    });
    expect(res.status).toBe(200);
    expect(calls).toEqual([
      {
        method: 'PUT',
        path: '/admin/world/records/document',
        body,
        extraHeaders: { 'x-ctx-lane': 'coe-world', 'X-Operator': 'claude-code' },
      },
    ]);
    expect(lastStash).toEqual({
      request_lane: 'coe-world',
      executed_lane: 'coe-world',
      record_path: '地方/甲.md',
      fingerprint_before: 'f1',
      fingerprint_after: 'f2',
      outcome: 'ok',
    });
  });

  it('DELETE document → DELETE /admin/world/records/document，下发 path 和 fingerprint', async () => {
    nextResult = { ok: { lane: 'coe-world', path: '甲.md', fingerprint: 'f1' } };
    await call('/api/ops/world/records/document?path=%E7%94%B2.md&fingerprint=f1', {
      method: 'DELETE',
      headers: { 'x-ctx-lane': 'coe-world' },
    });
    expect(calls).toEqual([
      {
        method: 'DELETE',
        path: '/admin/world/records/document',
        params: { path: '甲.md', fingerprint: 'f1' },
        extraHeaders: { 'x-ctx-lane': 'coe-world', 'X-Operator': 'claude-code' },
      },
    ]);
    expect(lastStash).toMatchObject({ record_path: '甲.md', fingerprint_before: 'f1', fingerprint_after: null, outcome: 'ok' });
  });

  it('请求体不是 JSON 对象时 400，不往下发', async () => {
    const res = await call('/api/ops/world/records/document', { method: 'PUT', body: '[1]' });
    expect(res.status).toBe(400);
    expect(calls).toEqual([]);
  });
});

describe('泳道与上游的回答', () => {
  it('没带泳道时不下发泳道头（落 prod），写操作仍带 X-Operator', async () => {
    await call('/api/ops/world/records/document', { method: 'PUT', body: { path: '甲.md', text: 'x' } });
    expect(calls[0].extraHeaders).toEqual({ 'X-Operator': 'claude-code' });
  });

  it('上游拒绝（409 指纹对不上）时状态码、上游的话和 lane 一起带回', async () => {
    nextResult = { fail: upstreamError(409, { lane: 'coe-world', message: '「甲.md」在读过之后被改过' }) };
    const res = await call('/api/ops/world/records/document', {
      method: 'PUT',
      headers: { 'x-lane': 'coe-world' },
      body: { path: '甲.md', text: 'x', fingerprint: 'old' },
    });
    expect(res.status).toBe(409);
    expect(res.body).toEqual({ message: '「甲.md」在读过之后被改过', lane: 'coe-world' });
    expect(lastStash).toMatchObject({
      request_lane: 'coe-world',
      executed_lane: 'coe-world',
      record_path: '甲.md',
      fingerprint_before: 'old',
      fingerprint_after: null,
      outcome: 'upstream_error',
    });
  });

  it('上游没回应时 504，不编造落点', async () => {
    const err = new Error('timeout exceeded') as Error & { code?: string };
    err.code = 'ECONNABORTED';
    nextResult = { fail: err };
    const res = await call('/api/ops/world/records', { headers: { 'x-lane': 'coe-world' } });
    expect(res.status).toBe(504);
    expect(res.body.lane).toBeUndefined();
    expect(lastStash).toMatchObject({ request_lane: 'coe-world', executed_lane: null, outcome: 'upstream_unavailable' });
  });
});

describe('审计动作名', () => {
  it('四条路由各有自己的动作名，路径不拼进动作名', async () => {
    const { deriveAction } = await import('../middleware/audit');
    expect(deriveAction('GET', '/dashboard/api/ops/world/records')).toBe('ops.world-records.list');
    expect(deriveAction('GET', '/dashboard/api/ops/world/records/document')).toBe('ops.world-records.read');
    expect(deriveAction('PUT', '/dashboard/api/ops/world/records/document')).toBe('ops.world-records.write');
    expect(deriveAction('DELETE', '/dashboard/api/ops/world/records/document')).toBe('ops.world-records.delete');
  });
});

// 人工写和删是人工改动唯一的留底：先落一条带正文的审计，落不下就不转发；转发之后补记结果。
// 这一条由路由自己写，共享的审计中间件对它们就不再写第二条（auditWritten）。
describe('写和删：先落审计再转发', () => {
  const body = { path: '地方/甲.md', text: '整份正文。', fingerprint: 'f1' };

  it('审计落不下就拒绝，不往下转发', async () => {
    auditBeginFails = true;
    const res = await call('/api/ops/world/records/document', { method: 'PUT', headers: { 'x-lane': 'coe-world' }, body });
    expect(res.status).toBe(503);
    expect(String(res.body.message)).toContain('审计');
    expect(calls).toEqual([]);
  });

  it('删除同样：审计落不下就不转发', async () => {
    auditBeginFails = true;
    const res = await call('/api/ops/world/records/document?path=%E7%94%B2.md&fingerprint=f1', { method: 'DELETE' });
    expect(res.status).toBe(503);
    expect(calls).toEqual([]);
  });

  it('先落一条带正文的审计，转发成功后补记结果', async () => {
    nextResult = { ok: { lane: 'coe-world', path: '地方/甲.md', fingerprint: 'f2', previous_fingerprint: 'f1', created: false } };
    const res = await call('/api/ops/world/records/document', { method: 'PUT', headers: { 'x-lane': 'coe-world' }, body });

    expect(res.status).toBe(200);
    expect(auditRows).toHaveLength(1);
    const [row] = auditRows;
    expect(row.caller).toBe('claude-code');
    expect(row.action).toBe('ops.world-records.write');
    expect(row.result).toBe('pending');
    expect(row.params.body).toEqual(body);
    expect(row.params.record_path).toBe('地方/甲.md');
    expect(calls).toHaveLength(1);
    expect(auditFinishes).toHaveLength(1);
    expect(auditFinishes[0].id).toBe(row.id);
    expect(auditFinishes[0].patch).toMatchObject({ result: 'success', error_message: null });
    expect(auditFinishes[0].patch.params).toMatchObject({
      body,
      executed_lane: 'coe-world',
      fingerprint_after: 'f2',
      outcome: 'ok',
    });
    expect(lastAuditWritten).toBe(true);
  });

  it('上游拒绝时补记成失败，带上上游的话', async () => {
    nextResult = { fail: upstreamError(409, { lane: 'coe-world', message: '指纹对不上' }) };
    const res = await call('/api/ops/world/records/document', { method: 'PUT', headers: { 'x-lane': 'coe-world' }, body });

    expect(res.status).toBe(409);
    expect(auditRows).toHaveLength(1);
    expect(auditFinishes[0].patch).toMatchObject({ result: 'error', error_message: '指纹对不上' });
    expect(auditFinishes[0].patch.params).toMatchObject({ outcome: 'upstream_error' });
  });

  it('上游没回应时补记成失败：改没改成未知，正文仍在审计里', async () => {
    const err = new Error('timeout exceeded') as Error & { code?: string };
    err.code = 'ECONNABORTED';
    nextResult = { fail: err };
    const res = await call('/api/ops/world/records/document', { method: 'PUT', body });

    expect(res.status).toBe(504);
    expect(auditRows[0].params.body).toEqual(body);
    expect(auditFinishes[0].patch).toMatchObject({ result: 'error' });
    expect(auditFinishes[0].patch.params).toMatchObject({ outcome: 'upstream_unavailable' });
  });

  it('补记结果失败不影响已经做成的写入，先落的那条正文还在', async () => {
    auditFinishFails = true;
    nextResult = { ok: { lane: 'coe-world', path: '地方/甲.md', fingerprint: 'f2', previous_fingerprint: 'f1', created: false } };
    const res = await call('/api/ops/world/records/document', { method: 'PUT', body });

    expect(res.status).toBe(200);
    expect(auditRows).toHaveLength(1);
    expect(auditRows[0].params.body).toEqual(body);
  });

  it('删除：先落审计（路径和指纹），转发后补记', async () => {
    nextResult = { ok: { lane: 'coe-world', path: '甲.md', fingerprint: 'f1' } };
    await call('/api/ops/world/records/document?path=%E7%94%B2.md&fingerprint=f1', { method: 'DELETE' });

    expect(auditRows).toHaveLength(1);
    expect(auditRows[0].action).toBe('ops.world-records.delete');
    expect(auditRows[0].params).toMatchObject({ record_path: '甲.md', fingerprint_before: 'f1' });
    expect(auditFinishes[0].patch).toMatchObject({ result: 'success' });
  });

  it('读和列目录照旧交给共享中间件审计，路由自己不写', async () => {
    nextResult = { ok: { lane: 'coe-world', records: [] } };
    await call('/api/ops/world/records');
    await call('/api/ops/world/records/document?path=%E7%94%B2.md');

    expect(auditRows).toEqual([]);
    expect(lastAuditWritten).toBeUndefined();
  });

  it('请求体不是 JSON 对象时不落审计、不转发，交给共享中间件记这次被拒', async () => {
    const res = await call('/api/ops/world/records/document', { method: 'PUT', body: '[1]' });
    expect(res.status).toBe(400);
    expect(auditRows).toEqual([]);
    expect(lastAuditWritten).toBeUndefined();
  });
});
