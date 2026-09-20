import { describe, it, expect, beforeEach } from 'bun:test';
import { Hono } from 'hono';

// ---------------------------------------------------------------------------
// Dashboard 转发 agent-service 的世界文档树管理端点（spec A5）。
//
// 三条钉死的东西：
// 1. 上游自报的 lane 原样透传。它说的是"这次调用实际改的是哪棵树"，是整条链路
//    选对树的唯一证据，不能吞掉、不能用请求里的泳道替换。
// 2. 泳道走 header（x-lane / x-ctx-lane 归一成 x-ctx-lane），不走 ?lane=。
//    query 参数不是选路，sidecar 只认 header，写成 query 会永远落在 prod。
// 3. 409 的上游 message 是写给模型看的中文（里面提 read_document 这只手），
//    HTTP 调用方没有这只手。对外措辞按 outcome 自己组织，上游原句另放一个字段。
// ---------------------------------------------------------------------------

import { createWorldDocumentsRoutes, type AgentDocumentsClient } from './world-documents';

type Call = {
  method: string;
  path: string;
  params?: unknown;
  body?: unknown;
  extraHeaders?: unknown;
};

const calls: Call[] = [];
let nextResult: { ok: unknown } | { fail: unknown } = { ok: {} };

function settle(): Promise<unknown> {
  if ('fail' in nextResult) return Promise.reject(nextResult.fail);
  return Promise.resolve(nextResult.ok);
}

const stubClient: AgentDocumentsClient = {
  get(path, params, extraHeaders) {
    calls.push({ method: 'GET', path, params, extraHeaders });
    return settle();
  },
  put(path, body, extraHeaders) {
    calls.push({ method: 'PUT', path, body, extraHeaders });
    return settle();
  },
  del(path, params, extraHeaders, body) {
    calls.push({ method: 'DELETE', path, params, extraHeaders, body });
    return settle();
  },
};

/** 上游（FastAPI HTTPException）的错误形状：axios 错误带 response.data.detail */
function upstreamError(status: number, detail: unknown): Error {
  const err = new Error(`Request failed with status code ${status}`) as Error & { response?: unknown };
  err.response = { status, data: { detail } };
  return err;
}

/** 上游没回应：axios 超时错误没有 response */
function timeoutError(): Error {
  const err = new Error('timeout of 15000ms exceeded') as Error & { code?: string };
  err.code = 'ECONNABORTED';
  return err;
}

/** 包一层把 handler stash 的审计字段取出来，供断言 */
let lastStash: Record<string, unknown> | undefined;

function buildApp() {
  const app = new Hono();
  app.use('*', async (c, next) => {
    await next();
    lastStash = c.get('gatewayAudit' as never) as Record<string, unknown> | undefined;
  });
  app.route('/', createWorldDocumentsRoutes(stubClient));
  return app;
}

async function call(
  path: string,
  init?: { method?: string; headers?: Record<string, string>; body?: unknown },
): Promise<{ status: number; body: Record<string, unknown> }> {
  const app = buildApp();
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
});

describe('下游路径与参数映射', () => {
  it('GET /api/ops/world-documents → GET /admin/world-documents/listing，带 under', async () => {
    nextResult = { ok: { lane: 'coe-living', under: '设定/', mounted: true, entries: ['设定/世界底子.md'] } };
    const res = await call('/api/ops/world-documents?under=' + encodeURIComponent('设定/'));
    expect(res.status).toBe(200);
    expect(calls).toEqual([
      { method: 'GET', path: '/admin/world-documents/listing', params: { under: '设定/' }, extraHeaders: undefined },
    ]);
    expect(res.body).toEqual({ lane: 'coe-living', under: '设定/', mounted: true, entries: ['设定/世界底子.md'] });
  });

  it('GET listing 不带 under 时不下发 under 参数', async () => {
    nextResult = { ok: { lane: 'prod', under: '', mounted: true, entries: [] } };
    await call('/api/ops/world-documents');
    expect(calls[0].params).toEqual({});
  });

  it('GET /api/ops/world-documents/document → GET /admin/world-documents/document，带 path', async () => {
    nextResult = { ok: { lane: 'coe-living', path: '设定/世界底子.md', fingerprint: '812f70471934', content: '第一版' } };
    const res = await call('/api/ops/world-documents/document?path=' + encodeURIComponent('设定/世界底子.md'));
    expect(res.status).toBe(200);
    expect(calls[0]).toMatchObject({
      method: 'GET',
      path: '/admin/world-documents/document',
      params: { path: '设定/世界底子.md' },
    });
    expect(res.body.content).toBe('第一版');
    expect(res.body.fingerprint).toBe('812f70471934');
  });

  it('PUT /api/ops/world-documents/document → PUT /admin/world-documents/document，带 path/content/fingerprint', async () => {
    nextResult = { ok: { lane: 'coe-living', path: '设定/世界底子.md', outcome: 'ok', fingerprint: 'aaaa11112222' } };
    const res = await call('/api/ops/world-documents/document', {
      method: 'PUT',
      body: { path: '设定/世界底子.md', content: '第二版', fingerprint: '812f70471934' },
    });
    expect(res.status).toBe(200);
    expect(calls[0]).toMatchObject({
      method: 'PUT',
      path: '/admin/world-documents/document',
      body: { path: '设定/世界底子.md', content: '第二版', fingerprint: '812f70471934' },
    });
    expect(res.body).toEqual({ lane: 'coe-living', path: '设定/世界底子.md', outcome: 'ok', fingerprint: 'aaaa11112222' });
  });

  it('PUT 不带 fingerprint 时下游 body 里没有 fingerprint 键（新建走这条）', async () => {
    nextResult = { ok: { lane: 'prod', path: 'a.md', outcome: 'ok', fingerprint: 'bbbb22223333' } };
    await call('/api/ops/world-documents/document', {
      method: 'PUT',
      body: { path: 'a.md', content: 'x' },
    });
    expect(Object.keys(calls[0].body as object).sort()).toEqual(['content', 'path']);
  });

  it('DELETE /api/ops/world-documents/document → DELETE /admin/world-documents/document，带 path/fingerprint', async () => {
    nextResult = { ok: { lane: 'coe-living', path: 'a.md', outcome: 'ok', fingerprint: '' } };
    const res = await call('/api/ops/world-documents/document?path=a.md&fingerprint=812f70471934', {
      method: 'DELETE',
    });
    expect(res.status).toBe(200);
    expect(calls[0]).toMatchObject({
      method: 'DELETE',
      path: '/admin/world-documents/document',
      params: { path: 'a.md', fingerprint: '812f70471934' },
    });
  });
});

describe('泳道走 header，不走 query', () => {
  it('x-lane 归一成 x-ctx-lane 转发（listing）', async () => {
    nextResult = { ok: { lane: 'coe-living', under: '', mounted: true, entries: [] } };
    await call('/api/ops/world-documents', { headers: { 'x-lane': 'coe-living' } });
    expect(calls[0].extraHeaders).toEqual({ 'x-ctx-lane': 'coe-living' });
  });

  it('x-ctx-lane 原样转发（PUT）', async () => {
    nextResult = { ok: { lane: 'coe-living', path: 'a.md', outcome: 'ok', fingerprint: 'c1' } };
    await call('/api/ops/world-documents/document', {
      method: 'PUT',
      headers: { 'x-ctx-lane': 'coe-living' },
      body: { path: 'a.md', content: 'x' },
    });
    expect(calls[0].extraHeaders).toEqual({ 'x-ctx-lane': 'coe-living' });
  });

  it('DELETE 也转发泳道 header', async () => {
    nextResult = { ok: { lane: 'coe-living', path: 'a.md', outcome: 'ok', fingerprint: '' } };
    await call('/api/ops/world-documents/document?path=a.md&fingerprint=f1', {
      method: 'DELETE',
      headers: { 'x-lane': 'coe-living' },
    });
    expect(calls[0].extraHeaders).toEqual({ 'x-ctx-lane': 'coe-living' });
  });

  it('GET document 也转发泳道 header', async () => {
    nextResult = { ok: { lane: 'coe-living', path: 'a.md', fingerprint: 'f1', content: 'x' } };
    await call('/api/ops/world-documents/document?path=a.md', { headers: { 'x-lane': 'coe-living' } });
    expect(calls[0].extraHeaders).toEqual({ 'x-ctx-lane': 'coe-living' });
  });

  it('没有泳道 header 时不编一个出来', async () => {
    nextResult = { ok: { lane: 'prod', under: '', mounted: true, entries: [] } };
    await call('/api/ops/world-documents');
    expect(calls[0].extraHeaders).toBeUndefined();
  });

  it('泳道不进下游 query（?lane= 是控制面写法，用在这里会永远落 prod）', async () => {
    nextResult = { ok: { lane: 'coe-living', under: '', mounted: true, entries: [] } };
    await call('/api/ops/world-documents?under=x', { headers: { 'x-lane': 'coe-living' } });
    const params = calls[0].params as Record<string, string>;
    expect(params.lane).toBeUndefined();
    expect(Object.keys(params)).toEqual(['under']);
  });
});

describe('上游自报的 lane 原样透传', () => {
  it('上游说 prod 而请求头写的是泳道时，返回的是上游说的 prod', async () => {
    nextResult = { ok: { lane: 'prod', path: 'a.md', fingerprint: 'f1', content: 'x' } };
    const res = await call('/api/ops/world-documents/document?path=a.md', {
      headers: { 'x-lane': 'coe-nonexistent' },
    });
    expect(res.body.lane).toBe('prod');
  });

  it('上游拒绝时 detail 里的 lane 也透传，且不被请求里的泳道替换', async () => {
    // 请求头写的是泳道、上游自报的是 prod——落回 prod 正是要被看见的那种情况，
    // 拒绝这条路径同样不能拿请求里的泳道顶替上游自报的值。
    nextResult = {
      fail: upstreamError(409, {
        lane: 'prod',
        path: 'a.md',
        outcome: 'stale_fingerprint',
        message: '这份文档在你读到之后被改过，先 read_document 读一遍再写回。',
      }),
    };
    const res = await call('/api/ops/world-documents/document', {
      method: 'PUT',
      headers: { 'x-lane': 'coe-living' },
      body: { path: 'a.md', content: 'x', fingerprint: 'stale' },
    });
    expect(res.body.lane).toBe('prod');
  });

  it('上游拒绝但没自报 lane 时，返回里就没有 lane（不拿请求里的泳道补位）', async () => {
    nextResult = { fail: upstreamError(500, { message: '上游炸了' }) };
    const res = await call('/api/ops/world-documents/document?path=a.md', {
      headers: { 'x-lane': 'coe-living' },
    });
    expect(res.status).toBe(500);
    expect('lane' in res.body).toBe(false);
  });

  it('上游没回应时不编 lane（宁可没有，不能拿请求里的泳道充数）', async () => {
    nextResult = { fail: timeoutError() };
    const res = await call('/api/ops/world-documents/document', {
      method: 'PUT',
      headers: { 'x-lane': 'coe-living' },
      body: { path: 'a.md', content: 'x' },
    });
    expect(res.status).toBe(504);
    expect('lane' in res.body).toBe(false);
  });
});

describe('参数校验（不打上游）', () => {
  const badCases: Array<[string, { method?: string; body?: unknown }, string]> = [
    ['GET document 缺 path', {}, '/api/ops/world-documents/document'],
    ['PUT 缺 path', { method: 'PUT', body: { content: 'x' } }, '/api/ops/world-documents/document'],
    ['PUT 缺 content', { method: 'PUT', body: { path: 'a.md' } }, '/api/ops/world-documents/document'],
    ['PUT path 为空串', { method: 'PUT', body: { path: '', content: 'x' } }, '/api/ops/world-documents/document'],
    ['PUT content 不是字符串', { method: 'PUT', body: { path: 'a.md', content: 1 } }, '/api/ops/world-documents/document'],
    ['PUT body 不是 JSON', { method: 'PUT', body: 'not json' }, '/api/ops/world-documents/document'],
    ['DELETE 缺 fingerprint', { method: 'DELETE' }, '/api/ops/world-documents/document?path=a.md'],
    ['DELETE 缺 path', { method: 'DELETE' }, '/api/ops/world-documents/document?fingerprint=f1'],
  ];

  for (const [name, init, path] of badCases) {
    it(`${name} → 400 且不打上游`, async () => {
      const res = await call(path, init as never);
      expect(res.status).toBe(400);
      expect(typeof res.body.message).toBe('string');
      expect(calls.length).toBe(0);
    });
  }
});

describe('上游拒绝的翻译', () => {
  const outcomes = ['no_fingerprint', 'stale_fingerprint', 'gone'] as const;

  for (const outcome of outcomes) {
    it(`409 ${outcome}：状态与 outcome 原样，措辞是自己的`, async () => {
      const upstreamMessage = `这里写给模型看：先 read_document 读一遍（${outcome}）`;
      nextResult = { fail: upstreamError(409, { lane: 'coe-living', path: 'a.md', outcome, message: upstreamMessage }) };
      const res = await call('/api/ops/world-documents/document', {
        method: 'PUT',
        body: { path: 'a.md', content: 'x', fingerprint: 'old' },
      });
      expect(res.status).toBe(409);
      expect(res.body.outcome).toBe(outcome);
      expect(res.body.lane).toBe('coe-living');
      expect(typeof res.body.message).toBe('string');
      // 对外措辞不是上游那句：上游那句里的工具名 HTTP 调用方没有
      expect(res.body.message).not.toBe(upstreamMessage);
      expect(res.body.message as string).not.toContain('read_document');
      // 上游原句仍然拿得到，但放在标明来源的字段里
      expect(res.body.upstream_message).toBe(upstreamMessage);
    });
  }

  it('gone 说的是整份没了、要重读，并给出重新建一份的出路', async () => {
    // 上游 GONE 是写和删两只手共用的：写那边是"你读到它之后它被删了"，删那边是
    // "你要删的那一份已经不在了"。对调用方是同一件事——那一份整个没了，重读再决定。
    // 不是"某个版本被删"，所以措辞不能说成版本。
    nextResult = { fail: upstreamError(409, { lane: 'l', path: 'a.md', outcome: 'gone', message: 'm' }) };
    const res = await call('/api/ops/world-documents/document', {
      method: 'PUT',
      body: { path: 'a.md', content: 'x', fingerprint: 'old' },
    });
    const message = res.body.message as string;
    expect(message).not.toContain('版本');
    expect(message).toContain('重新读');
    // 写的场景下真正的出路：不带指纹再来一次就是新建
    expect(message).toContain('不带指纹');
  });

  it('三种 outcome 的对外措辞互不相同（不是一句话糊过去）', async () => {
    const messages = new Set<string>();
    for (const outcome of outcomes) {
      nextResult = { fail: upstreamError(409, { lane: 'l', path: 'a.md', outcome, message: 'm' }) };
      const res = await call('/api/ops/world-documents/document', {
        method: 'PUT',
        body: { path: 'a.md', content: 'x', fingerprint: 'old' },
      });
      messages.add(res.body.message as string);
    }
    expect(messages.size).toBe(3);
  });

  it('400 透传状态与 lane', async () => {
    nextResult = { fail: upstreamError(400, { lane: 'coe-living', message: '路径不在根内' }) };
    const res = await call('/api/ops/world-documents/document', {
      method: 'PUT',
      body: { path: '../../etc/passwd', content: 'x' },
    });
    expect(res.status).toBe(400);
    expect(res.body.lane).toBe('coe-living');
    expect(res.body.upstream_message).toBe('路径不在根内');
  });

  it('404 透传状态与 lane', async () => {
    nextResult = { fail: upstreamError(404, { lane: 'coe-living', message: '没有这份文档' }) };
    const res = await call('/api/ops/world-documents/document?path=missing.md');
    expect(res.status).toBe(404);
    expect(res.body.lane).toBe('coe-living');
  });

  it('401（门没过）也带状态回来，不被吞成 500', async () => {
    nextResult = { fail: upstreamError(401, { message: 'unauthorized' }) };
    const res = await call('/api/ops/world-documents/document?path=a.md');
    expect(res.status).toBe(401);
  });

  it('上游超时 → 504', async () => {
    nextResult = { fail: timeoutError() };
    const res = await call('/api/ops/world-documents?under=x');
    expect(res.status).toBe(504);
    expect(typeof res.body.message).toBe('string');
  });
});

describe('handler 交给审计的结构化字段', () => {
  it('PUT 成功：路径、两个泳道、指纹、正文长度都在，正文不在', async () => {
    nextResult = { ok: { lane: 'coe-living', path: 'a.md', outcome: 'ok', fingerprint: 'newfp' } };
    await call('/api/ops/world-documents/document', {
      method: 'PUT',
      headers: { 'x-lane': 'coe-living' },
      body: { path: 'a.md', content: '12345', fingerprint: 'oldfp' },
    });
    expect(lastStash).toEqual({
      document_path: 'a.md',
      request_lane: 'coe-living',
      executed_lane: 'coe-living',
      fingerprint: 'oldfp',
      content_length: 5,
      outcome: 'ok',
    });
  });

  it('上游拒绝：outcome 与上游自报的 lane 落进审计字段', async () => {
    nextResult = {
      fail: upstreamError(409, { lane: 'prod', path: 'a.md', outcome: 'stale_fingerprint', message: 'm' }),
    };
    await call('/api/ops/world-documents/document', {
      method: 'PUT',
      headers: { 'x-lane': 'coe-living' },
      body: { path: 'a.md', content: 'xx', fingerprint: 'oldfp' },
    });
    expect(lastStash).toMatchObject({
      document_path: 'a.md',
      request_lane: 'coe-living',
      executed_lane: 'prod',
      fingerprint: 'oldfp',
      content_length: 2,
      outcome: 'stale_fingerprint',
    });
  });

  it('参数被拒：也留下记录，outcome 是 invalid_request', async () => {
    await call('/api/ops/world-documents/document', { method: 'PUT', body: { content: 'x' } });
    expect(lastStash).toMatchObject({ outcome: 'invalid_request' });
  });

  it('上游超时：executed_lane 是 null，不拿请求泳道充数', async () => {
    nextResult = { fail: timeoutError() };
    await call('/api/ops/world-documents/document', {
      method: 'PUT',
      headers: { 'x-lane': 'coe-living' },
      body: { path: 'a.md', content: 'x' },
    });
    expect(lastStash).toMatchObject({
      request_lane: 'coe-living',
      executed_lane: null,
      outcome: 'upstream_unavailable',
    });
  });
});
