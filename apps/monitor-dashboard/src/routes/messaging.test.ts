import { describe, it, expect, beforeEach } from 'bun:test';
import { Hono } from 'hono';

// ---------------------------------------------------------------------------
// Dashboard 转发 agent-service 的通信机制人工入口（/admin/messaging/*）。
//
// 开发机够不到 agent-service 的内网凭据，只能从这里过：这一侧认 PAAS_TOKEN、落审计，
// 带着 INNER_HTTP_SECRET 和泳道头转过去。钉死的几件事：
// 1. 路径和参数原样映射，请求体原样交下去（校验是上游的事，这里不重写规则）。
// 2. 泳道走 header：x-lane / x-ctx-lane 归一成 x-ctx-lane；不走 query。
// 3. 上游自报的 lane 原样透传；上游拒绝时状态码和 lane 一起带回来。
// 4. 提问要等对方回答，出站超时按提问方给的等待时长放宽，不能被默认的 15 秒截断。
// ---------------------------------------------------------------------------

import { createMessagingRoutes, type AgentMessagingClient } from './messaging';

type Call = {
  method: string;
  path: string;
  params?: unknown;
  body?: unknown;
  extraHeaders?: unknown;
  options?: unknown;
};

const calls: Call[] = [];
let nextResult: { ok: unknown } | { fail: unknown } = { ok: {} };

function settle(): Promise<unknown> {
  if ('fail' in nextResult) return Promise.reject(nextResult.fail);
  return Promise.resolve(nextResult.ok);
}

const stubClient: AgentMessagingClient = {
  get(path, params, extraHeaders) {
    calls.push({ method: 'GET', path, params, extraHeaders });
    return settle();
  },
  post(path, body, extraHeaders, options) {
    calls.push({ method: 'POST', path, body, extraHeaders, options });
    return settle();
  },
};

function upstreamError(status: number, detail: unknown): Error {
  const err = new Error(`Request failed with status code ${status}`) as Error & { response?: unknown };
  err.response = { status, data: { detail } };
  return err;
}

function timeoutError(): Error {
  const err = new Error('timeout exceeded') as Error & { code?: string };
  err.code = 'ECONNABORTED';
  return err;
}

let lastStash: Record<string, unknown> | undefined;

async function call(
  path: string,
  init?: { method?: string; headers?: Record<string, string>; body?: unknown },
): Promise<{ status: number; body: Record<string, unknown> }> {
  const app = new Hono();
  app.use('*', async (c, next) => {
    await next();
    lastStash = c.get('gatewayAudit' as never) as Record<string, unknown> | undefined;
  });
  app.route('/', createMessagingRoutes(stubClient));
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

describe('路径、参数、请求体', () => {
  it('POST send → POST /admin/messaging/send，请求体原样交下去', async () => {
    nextResult = { ok: { lane: 'coe-msg', message_id: 'm1', delivered: true, reason: null } };
    const body = { sender: 'akao', recipient: 'world', body: '我走进了厨房。' };
    const res = await call('/api/ops/messaging/send', {
      method: 'POST',
      headers: { 'x-lane': 'coe-msg' },
      body,
    });
    expect(res.status).toBe(200);
    expect(calls).toEqual([
      {
        method: 'POST',
        path: '/admin/messaging/send',
        body,
        extraHeaders: { 'x-ctx-lane': 'coe-msg' },
        options: undefined,
      },
    ]);
    expect(res.body).toEqual({ lane: 'coe-msg', message_id: 'm1', delivered: true, reason: null });
  });

  it('POST send-at → POST /admin/messaging/send-at', async () => {
    nextResult = { ok: { lane: 'coe-msg', message_id: 's1', deliver_at: '2026-09-29T18:30:00+08:00' } };
    const body = { sender: 'operator', recipient: 'operator', body: '提醒', at: '2026-09-29T18:30:00+08:00' };
    await call('/api/ops/messaging/send-at', { method: 'POST', headers: { 'x-ctx-lane': 'coe-msg' }, body });
    expect(calls[0]).toMatchObject({ method: 'POST', path: '/admin/messaging/send-at', body });
  });

  it('GET record → GET /admin/messaging/record，只下发给了的那几个查询参数', async () => {
    nextResult = { ok: { lane: 'coe-msg', rows: [] } };
    await call('/api/ops/messaging/record?participant=world&limit=20', { headers: { 'x-lane': 'coe-msg' } });
    expect(calls).toEqual([
      {
        method: 'GET',
        path: '/admin/messaging/record',
        params: { participant: 'world', limit: '20' },
        extraHeaders: { 'x-ctx-lane': 'coe-msg' },
      },
    ]);
  });

  it('参与者的名字可以是中文：请求体里的、查询参数里的都原样交下去', async () => {
    nextResult = { ok: { lane: 'coe-msg', message_id: 'm2', delivered: true, reason: null } };
    const body = { sender: '千凪', recipient: '赤尾', body: '姐姐，晚饭好了。' };
    await call('/api/ops/messaging/send', { method: 'POST', headers: { 'x-lane': 'coe-msg' }, body });
    await call(`/api/ops/messaging/record?participant=${encodeURIComponent('赤尾')}`, {
      headers: { 'x-lane': 'coe-msg' },
    });
    expect(calls[0]).toMatchObject({ method: 'POST', path: '/admin/messaging/send', body });
    expect(calls[1]).toMatchObject({
      method: 'GET',
      path: '/admin/messaging/record',
      params: { participant: '赤尾' },
    });
  });

  it('请求体不是 JSON 对象时 400，不往下发', async () => {
    const res = await call('/api/ops/messaging/send', { method: 'POST', body: '[1,2]' });
    expect(res.status).toBe(400);
    expect(calls).toEqual([]);
  });
});

describe('提问的等待时长', () => {
  it('出站超时比提问方愿意等的时长更长', async () => {
    nextResult = { ok: { lane: 'coe-msg', question_id: 'q1', answered: true, answer: '在。', reason: null } };
    await call('/api/ops/messaging/ask', {
      method: 'POST',
      body: { sender: 'operator', recipient: 'world', body: '在吗？', timeout_seconds: 120 },
    });
    const options = calls[0].options as { timeoutMs: number };
    expect(options.timeoutMs).toBeGreaterThan(120_000);
  });

  it('没给等待时长时按上游的默认 60 秒放宽', async () => {
    nextResult = { ok: {} };
    await call('/api/ops/messaging/ask', {
      method: 'POST',
      body: { sender: 'operator', recipient: 'world', body: '在吗？' },
    });
    const options = calls[0].options as { timeoutMs: number };
    expect(options.timeoutMs).toBeGreaterThan(60_000);
  });
});

describe('泳道与上游的回答', () => {
  it('没带泳道时不下发泳道头（落 prod）', async () => {
    await call('/api/ops/messaging/send', { method: 'POST', body: { sender: 'a', recipient: 'b', body: 'c' } });
    expect(calls[0].extraHeaders).toBeUndefined();
  });

  it('上游拒绝（409 泳道不符）时状态码和上游自报的 lane 一起带回', async () => {
    nextResult = {
      fail: upstreamError(409, { lane: 'prod', message: 'request was meant for lane coe-msg but reached lane prod; nothing was sent' }),
    };
    const res = await call('/api/ops/messaging/send', {
      method: 'POST',
      headers: { 'x-lane': 'coe-msg' },
      body: { sender: 'a', recipient: 'b', body: 'c' },
    });
    expect(res.status).toBe(409);
    expect(res.body.lane).toBe('prod');
    expect(String(res.body.message)).toContain('nothing was sent');
    expect(lastStash).toMatchObject({ request_lane: 'coe-msg', executed_lane: 'prod', outcome: 'upstream_error' });
  });

  it('上游发送失败时，回答里原样带回消息 id，调用方才能沿用它重试', async () => {
    nextResult = {
      fail: upstreamError(503, {
        lane: 'coe-msg',
        message: 'could not record message m-9 (delivered): COMMIT failed',
        message_id: 'm-9',
      }),
    };
    const res = await call('/api/ops/messaging/send', {
      method: 'POST',
      headers: { 'x-lane': 'coe-msg' },
      body: { sender: 'a', recipient: 'b', body: 'c' },
    });
    expect(res.status).toBe(503);
    expect(res.body.message_id).toBe('m-9');
    expect(lastStash).toMatchObject({ message_id: 'm-9', outcome: 'upstream_error' });
  });

  it('上游没回应时 504，不编造落点', async () => {
    nextResult = { fail: timeoutError() };
    const res = await call('/api/ops/messaging/send', {
      method: 'POST',
      headers: { 'x-lane': 'coe-msg' },
      body: { sender: 'a', recipient: 'b', body: 'c' },
    });
    expect(res.status).toBe(504);
    expect(res.body.lane).toBeUndefined();
    expect(lastStash).toMatchObject({ request_lane: 'coe-msg', executed_lane: null, outcome: 'upstream_unavailable' });
  });

  it('成功时审计里记下请求泳道、执行泳道和消息 id', async () => {
    nextResult = { ok: { lane: 'coe-msg', message_id: 'm9', delivered: false, reason: '对方没有开设收件箱' } };
    await call('/api/ops/messaging/send', {
      method: 'POST',
      headers: { 'x-ctx-lane': 'coe-msg' },
      body: { sender: 'a', recipient: 'nobody', body: 'c' },
    });
    expect(lastStash).toMatchObject({
      request_lane: 'coe-msg',
      executed_lane: 'coe-msg',
      message_id: 'm9',
      outcome: 'ok',
    });
  });
});

describe('本泳道的死信', () => {
  it('GET dead-letters → GET /admin/messaging/dead-letters，带 limit 与泳道头', async () => {
    nextResult = { ok: { lane: 'coe-msg', dead_letters: [] } };
    await call('/api/ops/messaging/dead-letters?limit=5', { headers: { 'x-lane': 'coe-msg' } });
    expect(calls).toEqual([
      {
        method: 'GET',
        path: '/admin/messaging/dead-letters',
        params: { limit: '5' },
        extraHeaders: { 'x-ctx-lane': 'coe-msg' },
      },
    ]);
  });

  it('POST dead-letters/replay → POST /admin/messaging/dead-letters/replay，调用者作为 X-Operator 带下去', async () => {
    nextResult = { ok: { lane: 'coe-msg', replayed: 1, refused: 0, failed: 0 } };
    const app = new Hono();
    app.use('*', async (c, next) => {
      c.set('caller' as never, 'bezhai' as never);
      await next();
    });
    app.route('/', createMessagingRoutes(stubClient));
    const res = await app.request('/api/ops/messaging/dead-letters/replay', {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'x-lane': 'coe-msg' },
      body: JSON.stringify({ limit: 3 }),
    });
    expect(res.status).toBe(200);
    expect(calls).toEqual([
      {
        method: 'POST',
        path: '/admin/messaging/dead-letters/replay',
        body: { limit: 3 },
        extraHeaders: { 'x-ctx-lane': 'coe-msg', 'X-Operator': 'bezhai' },
        options: undefined,
      },
    ]);
  });
});
