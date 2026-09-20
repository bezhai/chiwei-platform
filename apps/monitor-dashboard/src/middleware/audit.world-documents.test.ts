import { describe, it, expect, mock, beforeEach, afterAll } from 'bun:test';
import jwt from 'jsonwebtoken';

// ---------------------------------------------------------------------------
// 审计里不能出现文档正文（spec A5），**跑在生产的 app 组装上**。
//
// 这组用例不自己搭 Hono app：它用 createDashboardApp()，也就是 index.ts 启动时用的
// 那一个，中间件顺序、挂载前缀、路由注册全都是生产的那份。自搭一个的话验到的是自搭
// 的场景，中间件顺序一改就不再是同一条路，而且不会有任何东西变红。
//
// 上游也是真的：起一个本地 HTTP server 扮 agent-service，请求经真实的 axios client
// 打过去，所以上游各种状态码走的是真实的错误形状。
//
// 一条判据：**零条审计记录不等于脱敏成功。** 零记录可能意味着这条路径压根没被审计
// 覆盖。所以每条分支都先断言"落没落库"，再断言"落库的那条里没有正文"。
// ---------------------------------------------------------------------------

// 捕获落库对象。bun 的 mock.module 是进程级的，替换掉的是整个模块——所以要把
// '../db' 原有的导出（各 entity）带上，只换 AppDataSource，否则别的测试文件从
// '../db' 取 entity 会在某些执行顺序下直接报 Export not found。
const savedRows: Array<Record<string, unknown>> = [];
const actualDb = await import('../db');
mock.module('../db', () => ({
  ...actualDb,
  AppDataSource: {
    getRepository: () => ({
      save: async (row: Record<string, unknown>) => {
        savedRows.push(row);
        return row;
      },
    }),
  },
}));

const JWT_SECRET = 'audit-test-jwt-secret';
const CC_TOKEN = 'audit-test-cc-token';
process.env.DASHBOARD_JWT_SECRET = JWT_SECRET;
process.env.DASHBOARD_CC_TOKEN = CC_TOKEN;
process.env.INNER_HTTP_SECRET = 'audit-test-inner-secret';

/** 扮 agent-service 的本地 server */
let nextUpstream: { status: number; body: unknown } = { status: 200, body: {} };
const upstream = Bun.serve({
  port: 0,
  fetch() {
    return new Response(JSON.stringify(nextUpstream.body), {
      status: nextUpstream.status,
      headers: { 'content-type': 'application/json' },
    });
  },
});
const UPSTREAM_URL = `http://127.0.0.1:${upstream.port}`;

/** 一个确定关着的端口，用来造"上游连不上" */
const deadServer = Bun.serve({ port: 0, fetch: () => new Response('') });
const DEAD_URL = `http://127.0.0.1:${deadServer.port}`;
deadServer.stop(true);

process.env.DASHBOARD_AGENT_API = UPSTREAM_URL;

const { createDashboardApp } = await import('../app');
const { deriveAction, excludesRawInput } = await import('./audit');
const { createWorldDocumentsRoutes } = await import('../routes/world-documents');

const app = createDashboardApp();

afterAll(() => {
  upstream.stop(true);
});

/** 一段只出现在正文里的标记串，用来 grep 整条落库记录 */
const MARKER = 'WORLD_DOC_BODY_MARKER_8f3a2b';
const BODY_WITH_MARKER = { path: 'a.md', content: `正文开头 ${MARKER} 正文结尾`, fingerprint: 'oldfp' };

const AS_API_KEY = { 'x-api-key': CC_TOKEN };
const AS_WEB_ADMIN = { authorization: `Bearer ${jwt.sign({ sub: 'bezhai' }, JWT_SECRET)}` };

async function call(
  path: string,
  init: { method?: string; headers?: Record<string, string>; body?: unknown } = {},
): Promise<{ status: number; body: Record<string, unknown> }> {
  const headers: Record<string, string> = { ...(init.headers || {}) };
  const req: RequestInit = { method: init.method || 'GET' };
  if (init.body !== undefined) {
    headers['content-type'] = 'application/json';
    req.body = typeof init.body === 'string' ? init.body : JSON.stringify(init.body);
  }
  req.headers = headers;
  const res = await app.request(`http://localhost${path}`, req);
  let body: Record<string, unknown> = {};
  try {
    body = (await res.json()) as Record<string, unknown>;
  } catch {
    /* empty */
  }
  return { status: res.status, body };
}

/** 落了几条？落的那条里有没有标记串？ */
function auditOutcome(): { landed: number; serialized: string; hasMarker: boolean; row?: Record<string, unknown> } {
  const serialized = JSON.stringify(savedRows);
  return {
    landed: savedRows.length,
    serialized,
    hasMarker: serialized.includes(MARKER),
    row: savedRows[0],
  };
}

beforeEach(() => {
  savedRows.length = 0;
  nextUpstream = { status: 200, body: { lane: 'coe-living', path: 'a.md', outcome: 'ok', fingerprint: 'newfp' } };
  process.env.DASHBOARD_AGENT_API = UPSTREAM_URL;
});

// ---------------------------------------------------------------------------

describe('deriveAction：world-documents 路径映射（动作名里不含文档路径）', () => {
  const cases: Array<[string, string, string]> = [
    ['GET', '/dashboard/api/ops/world-documents', 'ops.world-documents.list'],
    ['GET', '/dashboard/api/ops/world-documents/document', 'ops.world-documents.read'],
    ['PUT', '/dashboard/api/ops/world-documents/document', 'ops.world-documents.write'],
    ['DELETE', '/dashboard/api/ops/world-documents/document', 'ops.world-documents.delete'],
  ];

  for (const [method, path, expected] of cases) {
    it(`${method} ${path} → ${expected}`, () => {
      expect(deriveAction(method, path)).toBe(expected);
    });
  }

  it('动作名都在 audit_logs.action 的 varchar(100) 之内', () => {
    for (const [method, path] of cases) {
      expect(deriveAction(method, path).length).toBeLessThanOrEqual(100);
    }
  });

  it('前缀下走不到路由的长路径也不撑爆 action（文档路径是变长多段的）', () => {
    const longPath = '/dashboard/api/ops/world-documents/' + '设定/世界底子与它的很长的名字'.repeat(8) + '.md';
    const action = deriveAction('GET', longPath);
    expect(action.length).toBeLessThanOrEqual(100);
    expect(action).toBe('ops.world-documents.unknown');
  });
});

// ---------------------------------------------------------------------------

describe('handler 之前就返回的路径：审计到底记没记', () => {
  it('没凭据 → 401，零条审计；而同一个请求带上凭据会落一条（零记录不等于脱敏成功）', async () => {
    // 生产顺序是 jwtAuth 在 auditMiddleware 之前，它拒绝时审计中间件根本没跑。
    // 只断言"没有正文"会让这条路径看起来是绿的，其实它压根没被审计覆盖——所以
    // 这里把对照写进同一条用例：不带凭据 0 条，带凭据 1 条。
    const denied = await call('/dashboard/api/ops/world-documents/document', {
      method: 'PUT',
      body: BODY_WITH_MARKER,
    });
    expect(denied.status).toBe(401);
    const a = auditOutcome();
    expect(a.landed).toBe(0);
    expect(a.hasMarker).toBe(false);

    savedRows.length = 0;
    const allowed = await call('/dashboard/api/ops/world-documents/document', {
      method: 'PUT',
      headers: AS_API_KEY,
      body: BODY_WITH_MARKER,
    });
    expect(allowed.status).toBe(200);
    const b = auditOutcome();
    expect(b.landed).toBe(1);
    expect(b.hasMarker).toBe(false);
  });

  it('凭据是错的 → 401，零条审计', async () => {
    const res = await call('/dashboard/api/ops/world-documents/document', {
      method: 'PUT',
      headers: { 'x-api-key': 'wrong-token' },
      body: BODY_WITH_MARKER,
    });
    expect(res.status).toBe(401);
    const a = auditOutcome();
    expect(a.landed).toBe(0);
    expect(a.hasMarker).toBe(false);
  });

  it('Bearer 是伪造的 → 401，零条审计', async () => {
    const res = await call('/dashboard/api/ops/world-documents/document', {
      method: 'PUT',
      headers: { authorization: `Bearer ${jwt.sign({ sub: 'x' }, 'wrong-secret')}` },
      body: BODY_WITH_MARKER,
    });
    expect(res.status).toBe(401);
    expect(auditOutcome().landed).toBe(0);
  });

  it('已认证但路由没匹配上（handler 一次都没跑）→ 404，落一条，里面没有正文', async () => {
    const res = await call(
      `/dashboard/api/ops/world-documents/no-such-endpoint?content=${encodeURIComponent(MARKER)}`,
      { method: 'PUT', headers: AS_API_KEY, body: BODY_WITH_MARKER },
    );
    expect(res.status).toBe(404);
    const a = auditOutcome();
    expect(a.landed).toBe(1);
    expect(a.hasMarker).toBe(false);
    expect(a.row!.action).toBe('ops.world-documents.unknown');
    // handler 没跑过，所以结构化字段也没有——但原始 body / query 同样没进去
    const params = (a.row!.params ?? {}) as Record<string, unknown>;
    expect(params.body).toBeUndefined();
    expect(params.query).toBeUndefined();
  });

  it('world-documents 之外的未匹配路径仍然照常记 body 与 query（排除是限定范围的）', async () => {
    const res = await call('/dashboard/api/ops/no-such-thing?limit=10', {
      method: 'POST',
      headers: AS_API_KEY,
      body: { note: 'kept' },
    });
    expect(res.status).toBe(404);
    const a = auditOutcome();
    expect(a.landed).toBe(1);
    const params = a.row!.params as Record<string, unknown>;
    expect(params.body).toEqual({ note: 'kept' });
    expect(params.query).toEqual({ limit: ['10'] });
  });
});

// ---------------------------------------------------------------------------

describe('落库记录里没有文档正文：逐分支（生产组装）', () => {
  it('成功写入（x-api-key 调用者）', async () => {
    const res = await call('/dashboard/api/ops/world-documents/document', {
      method: 'PUT',
      headers: { ...AS_API_KEY, 'x-lane': 'coe-living' },
      body: BODY_WITH_MARKER,
    });
    expect(res.status).toBe(200);
    const a = auditOutcome();
    expect(a.landed).toBe(1);
    expect(a.hasMarker).toBe(false);
    expect(a.row!.caller).toBe('claude-code');
    expect(a.row!.action).toBe('ops.world-documents.write');
    const params = a.row!.params as Record<string, unknown>;
    expect(params.document_path).toBe('a.md');
    expect(params.request_lane).toBe('coe-living');
    expect(params.executed_lane).toBe('coe-living');
    expect(params.fingerprint).toBe('oldfp');
    expect(params.content_length).toBe(BODY_WITH_MARKER.content.length);
    expect(params.body).toBeUndefined();
  });

  it('成功写入（JWT 调用者）', async () => {
    const res = await call('/dashboard/api/ops/world-documents/document', {
      method: 'PUT',
      headers: { ...AS_WEB_ADMIN, 'x-lane': 'coe-living' },
      body: BODY_WITH_MARKER,
    });
    expect(res.status).toBe(200);
    const a = auditOutcome();
    expect(a.landed).toBe(1);
    expect(a.hasMarker).toBe(false);
    expect(a.row!.caller).toBe('web-admin');
  });

  it('handler 内的参数校验提前返回（注意：这条仍然进了 handler）', async () => {
    const res = await call('/dashboard/api/ops/world-documents/document', {
      method: 'PUT',
      headers: AS_API_KEY,
      body: { content: `${MARKER}` },
    });
    expect(res.status).toBe(400);
    const a = auditOutcome();
    expect(a.landed).toBe(1);
    expect(a.hasMarker).toBe(false);
    expect((a.row!.params as Record<string, unknown>).outcome).toBe('invalid_request');
  });

  const upstreamStatuses: Array<[number, unknown, string]> = [
    [400, { lane: 'coe-living', message: `参数不合法：${MARKER}` }, 'upstream_error'],
    [401, { message: `门没过：${MARKER}` }, 'upstream_error'],
    [403, { lane: 'coe-living', message: `没权限：${MARKER}` }, 'upstream_error'],
    [404, { lane: 'coe-living', message: `没有这份：${MARKER}` }, 'upstream_error'],
    [409, { lane: 'coe-living', path: 'a.md', outcome: 'stale_fingerprint', message: `先 read_document：${MARKER}` }, 'stale_fingerprint'],
    [422, { lane: 'coe-living', message: `校验失败：${MARKER}` }, 'upstream_error'],
    [500, { message: `上游炸了：${MARKER}` }, 'upstream_error'],
    [503, { message: `上游不可用：${MARKER}` }, 'upstream_error'],
  ];

  for (const [status, detail, expectedOutcome] of upstreamStatuses) {
    it(`上游 ${status}：透传状态、落一条、没有正文`, async () => {
      nextUpstream = { status, body: { detail } };
      const res = await call('/dashboard/api/ops/world-documents/document', {
        method: 'PUT',
        headers: { ...AS_API_KEY, 'x-lane': 'coe-living' },
        body: BODY_WITH_MARKER,
      });
      expect(res.status).toBe(status);
      const a = auditOutcome();
      expect(a.landed).toBe(1);
      expect(a.hasMarker).toBe(false);
      expect((a.row!.params as Record<string, unknown>).outcome).toBe(expectedOutcome);
    });
  }

  it('上游返回的不是 JSON（502 之类的中间层页面）也不泄露正文', async () => {
    nextUpstream = { status: 502, body: `<html>${MARKER}</html>` };
    const res = await call('/dashboard/api/ops/world-documents/document', {
      method: 'PUT',
      headers: AS_API_KEY,
      body: BODY_WITH_MARKER,
    });
    expect(res.status).toBe(502);
    const a = auditOutcome();
    expect(a.landed).toBe(1);
    expect(a.hasMarker).toBe(false);
  });

  it('上游连不上 → 504，落一条，没有正文', async () => {
    process.env.DASHBOARD_AGENT_API = DEAD_URL;
    const res = await call('/dashboard/api/ops/world-documents/document', {
      method: 'PUT',
      headers: { ...AS_API_KEY, 'x-lane': 'coe-living' },
      body: BODY_WITH_MARKER,
    });
    expect(res.status).toBe(504);
    const a = auditOutcome();
    expect(a.landed).toBe(1);
    expect(a.hasMarker).toBe(false);
    const params = a.row!.params as Record<string, unknown>;
    expect(params.outcome).toBe('upstream_unavailable');
    expect(params.executed_lane).toBeNull();
  });

  it('查询串这条通道：调用方往 query 里塞正文也不落库', async () => {
    nextUpstream = { status: 200, body: { lane: 'coe-living', path: 'a.md', fingerprint: 'f1', content: 'x' } };
    const res = await call(
      `/dashboard/api/ops/world-documents/document?path=a.md&content=${encodeURIComponent(MARKER)}`,
      { headers: AS_API_KEY },
    );
    expect(res.status).toBe(200);
    const a = auditOutcome();
    expect(a.landed).toBe(1);
    expect(a.hasMarker).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// 排除清单和路由实际注册在哪儿是两处独立决定的东西，能各自漂移。漂了之后正文开始
// 静默进审计——不报错、没有别的用例会红。所以这里不自己写一遍路径字面量，而是从
// **实际注册的路由表**枚举。
// ---------------------------------------------------------------------------

const registeredRoutes = createWorldDocumentsRoutes({
  get: async () => ({}),
  put: async () => ({}),
  del: async () => ({}),
} as never).routes;

describe('排除清单跟着实际注册的路由走', () => {
  it('路由表非空，且覆盖读/列/写/删四个注册', () => {
    expect(registeredRoutes.length).toBeGreaterThanOrEqual(4);
    expect(new Set(registeredRoutes.map((r) => r.method))).toEqual(new Set(['GET', 'PUT', 'DELETE']));
  });

  for (const route of registeredRoutes) {
    it(`${route.method} ${route.path} 在排除清单内`, () => {
      expect(excludesRawInput(route.path)).toBe(true);
      expect(excludesRawInput(`/dashboard${route.path}`)).toBe(true);
    });
  }

  for (const route of registeredRoutes) {
    it(`${route.method} ${route.path} 走生产链路时 body 与 query 都不落库`, async () => {
      const url = `/dashboard${route.path}?path=a.md&fingerprint=f1&content=${encodeURIComponent(MARKER)}`;
      const init: { method: string; headers: Record<string, string>; body?: unknown } = {
        method: route.method,
        headers: AS_API_KEY,
      };
      if (route.method !== 'GET') init.body = BODY_WITH_MARKER;

      const res = await call(url, init);
      expect(res.status).toBe(200);
      const a = auditOutcome();
      expect(a.landed).toBe(1);
      expect(a.hasMarker).toBe(false);
    });
  }
});
