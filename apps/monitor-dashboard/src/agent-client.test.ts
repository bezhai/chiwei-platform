import { describe, it, expect, afterEach } from 'bun:test';
import { getAgentConfig, agentClient, createClient, getWorldConfig, worldClient } from './paas-client';

// 钉死打 agent-service 那个出站 client 的配置：地址来自 DASHBOARD_AGENT_API，
// 凭据是内网互信那把 INNER_HTTP_SECRET，走 Authorization: Bearer。
// 现有的 paasClient / channelClient 都指着别的服务、用的是 X-API-Key，不能复用。

const saved = {
  api: process.env.DASHBOARD_AGENT_API,
  worldApi: process.env.DASHBOARD_WORLD_API,
  secret: process.env.INNER_HTTP_SECRET,
  paasApi: process.env.DASHBOARD_PAAS_API,
  paasToken: process.env.DASHBOARD_PAAS_TOKEN,
};

afterEach(() => {
  if (saved.api === undefined) delete process.env.DASHBOARD_AGENT_API;
  else process.env.DASHBOARD_AGENT_API = saved.api;
  if (saved.worldApi === undefined) delete process.env.DASHBOARD_WORLD_API;
  else process.env.DASHBOARD_WORLD_API = saved.worldApi;
  if (saved.secret === undefined) delete process.env.INNER_HTTP_SECRET;
  else process.env.INNER_HTTP_SECRET = saved.secret;
  if (saved.paasApi === undefined) delete process.env.DASHBOARD_PAAS_API;
  else process.env.DASHBOARD_PAAS_API = saved.paasApi;
  if (saved.paasToken === undefined) delete process.env.DASHBOARD_PAAS_TOKEN;
  else process.env.DASHBOARD_PAAS_TOKEN = saved.paasToken;
});

describe('agent-service 出站 client 配置', () => {
  it('默认打 http://agent-service:8000，带 Bearer', () => {
    delete process.env.DASHBOARD_AGENT_API;
    process.env.INNER_HTTP_SECRET = 's3cr3t';
    expect(getAgentConfig()).toEqual({
      baseURL: 'http://agent-service:8000',
      headers: { Authorization: 'Bearer s3cr3t' },
    });
  });

  it('DASHBOARD_AGENT_API 覆盖默认地址', () => {
    process.env.DASHBOARD_AGENT_API = 'http://agent-service-coe-living:8000';
    process.env.INNER_HTTP_SECRET = 's3cr3t';
    expect(getAgentConfig().baseURL).toBe('http://agent-service-coe-living:8000');
  });

  it('没有 INNER_HTTP_SECRET 时直接报错，不发裸请求', () => {
    process.env.DASHBOARD_AGENT_API = 'http://agent-service:8000';
    delete process.env.INNER_HTTP_SECRET;
    expect(() => getAgentConfig()).toThrow('INNER_HTTP_SECRET');
  });
});

// 上面那几条只证明配置对，证明不了真发出去的请求带着这两个 header。这里起一个真
// 的 HTTP server 收一次，把 Authorization 和 x-ctx-lane 从实际报文里读出来。
describe('agentClient 真发出去的请求', () => {
  it('同时带上 Bearer 与 x-ctx-lane，且响应里的 lane 不被 unwrap 吃掉', async () => {
    const seen: Array<{ url: string; auth: string | null; lane: string | null }> = [];
    const server = Bun.serve({
      port: 0,
      fetch(req) {
        seen.push({
          url: new URL(req.url).pathname + new URL(req.url).search,
          auth: req.headers.get('authorization'),
          lane: req.headers.get('x-ctx-lane'),
        });
        return Response.json({ lane: 'coe-living', records: [] });
      },
    });

    try {
      process.env.DASHBOARD_AGENT_API = `http://127.0.0.1:${server.port}`;
      process.env.INNER_HTTP_SECRET = 's3cr3t';

      const data = await agentClient.get(
        '/admin/messaging/record',
        { participant: 'world' },
        { 'x-ctx-lane': 'coe-living' },
      );

      expect(seen.length).toBe(1);
      expect(seen[0].url).toBe('/admin/messaging/record?participant=world');
      expect(seen[0].auth).toBe('Bearer s3cr3t');
      expect(seen[0].lane).toBe('coe-living');
      expect(data).toEqual({ lane: 'coe-living', records: [] });
    } finally {
      server.stop(true);
    }
  });
});

// 参与者的名字可以是中文（赤尾、千凪）。查询参数要按 UTF-8 编码发出去，上游解回来的还是原来的名字。
describe('agentClient 发中文的查询参数', () => {
  it('上游从实际报文里解出来的 participant 就是原来的中文名', async () => {
    const seen: Array<{ raw: string; participant: string | null }> = [];
    const server = Bun.serve({
      port: 0,
      fetch(req) {
        const url = new URL(req.url);
        seen.push({ raw: url.search, participant: url.searchParams.get('participant') });
        return Response.json({ lane: 'coe-living', rows: [] });
      },
    });
    try {
      process.env.DASHBOARD_AGENT_API = `http://127.0.0.1:${server.port}`;
      process.env.INNER_HTTP_SECRET = 's3cr3t';

      await agentClient.get('/admin/messaging/record', { participant: '赤尾' });

      expect(seen).toEqual([{ raw: '?participant=%E8%B5%A4%E5%B0%BE', participant: '赤尾' }]);
    } finally {
      server.stop(true);
    }
  });
});

// createClient 默认会把顶层带 data 键的响应拆开只返回 data（那是 paas-engine 的信封
// 口径）。agent-service 不是那个口径：上游哪天加一个 data 字段，lane 会被静默吃掉，
// 而 lane 是整条链路唯一能证明"这次操作落在哪条泳道"的东西。所以 agentClient 直接透传。
describe('agentClient 不拆包', () => {
  it('上游响应顶层带 data 键时，整个对象原样返回，lane 不被吃掉', async () => {
    const upstreamBody = {
      lane: 'coe-living',
      records: [],
      data: { 这是上游自己的一个字段: '不是信封' },
    };
    const server = Bun.serve({ port: 0, fetch: () => Response.json(upstreamBody) });
    try {
      process.env.DASHBOARD_AGENT_API = `http://127.0.0.1:${server.port}`;
      process.env.INNER_HTTP_SECRET = 's3cr3t';

      const got = await agentClient.get('/admin/messaging/record', { participant: 'world' });
      expect(got).toEqual(upstreamBody);
      expect((got as Record<string, unknown>).lane).toBe('coe-living');
    } finally {
      server.stop(true);
    }
  });

  it('默认仍然按 paas-engine 的信封口径拆包（新增的选项没改默认行为）', async () => {
    // 直接用工厂造两个 client 对比，不碰 paasClient 那个单例——它被别的测试文件
    // 进程级 mock 掉了，拿它断言会变成跟执行顺序有关。
    const server = Bun.serve({ port: 0, fetch: () => Response.json({ data: { name: 'r1' } }) });
    try {
      const config = () => ({ baseURL: `http://127.0.0.1:${server.port}`, headers: {} });
      const enveloped = createClient(config);
      const passthrough = createClient(config, { envelope: false });

      expect(await enveloped.get('/x')).toEqual({ name: 'r1' });
      expect(await passthrough.get('/x')).toEqual({ data: { name: 'r1' } });
    } finally {
      server.stop(true);
    }
  });
});

// 提问要等对方回答，默认的 15 秒出站超时会把一次正常的等待截断成"上游没回应"。
describe('单次调用可以放宽出站超时', () => {
  it('post 的 timeoutMs 生效：给得比上游慢就超时，给得够就拿到回答', async () => {
    const server = Bun.serve({
      port: 0,
      async fetch() {
        await Bun.sleep(300);
        return Response.json({ lane: 'coe-msg', answered: true });
      },
    });
    try {
      process.env.DASHBOARD_AGENT_API = `http://127.0.0.1:${server.port}`;
      process.env.INNER_HTTP_SECRET = 's3cr3t';

      await expect(
        agentClient.post('/admin/messaging/ask', {}, undefined, { timeoutMs: 50 }),
      ).rejects.toThrow();
      const data = await agentClient.post('/admin/messaging/ask', {}, undefined, { timeoutMs: 5000 });
      expect(data).toEqual({ lane: 'coe-msg', answered: true });
    } finally {
      server.stop(true);
    }
  });
});

// world 是同一个镜像上的另一个 App，服务名是 world，不是 agent-service。它的人工接口认的
// 是同一把内网凭据；泳道照样靠 x-ctx-lane 交给 sidecar 选路（world-<泳道>）。
describe('world 出站 client', () => {
  it('默认打 world 服务的 8000 端口，带 Bearer；DASHBOARD_WORLD_API 覆盖地址', () => {
    delete process.env.DASHBOARD_WORLD_API;
    process.env.INNER_HTTP_SECRET = 's3cr3t';
    expect(getWorldConfig()).toEqual({
      baseURL: ['http:', '', 'world:8000'].join('/'),
      headers: { Authorization: 'Bearer s3cr3t' },
    });
    process.env.DASHBOARD_WORLD_API = 'http://elsewhere:1';
    expect(getWorldConfig().baseURL).toBe('http://elsewhere:1');
  });

  it('没有 INNER_HTTP_SECRET 时直接报错，不发裸请求', () => {
    delete process.env.INNER_HTTP_SECRET;
    expect(() => getWorldConfig()).toThrow('INNER_HTTP_SECRET');
  });

  it('真发出去的删除请求带着 Bearer、x-ctx-lane、X-Operator 和查询参数，响应不拆包', async () => {
    const seen: Array<{ method: string; url: string; auth: string | null; lane: string | null; operator: string | null }> = [];
    const server = Bun.serve({
      port: 0,
      fetch(req) {
        const url = new URL(req.url);
        seen.push({
          method: req.method,
          url: url.pathname + url.search,
          auth: req.headers.get('authorization'),
          lane: req.headers.get('x-ctx-lane'),
          operator: req.headers.get('x-operator'),
        });
        return Response.json({ lane: 'coe-world', path: '甲.md', fingerprint: 'f1', data: 'x' });
      },
    });
    try {
      process.env.DASHBOARD_WORLD_API = `http://127.0.0.1:${server.port}`;
      process.env.INNER_HTTP_SECRET = 's3cr3t';

      const data = await worldClient.del(
        '/admin/world/records/document',
        { path: '甲.md', fingerprint: 'f1' },
        { 'x-ctx-lane': 'coe-world', 'X-Operator': 'claude-code' },
      );

      expect(seen).toEqual([
        {
          method: 'DELETE',
          url: '/admin/world/records/document?path=%E7%94%B2.md&fingerprint=f1',
          auth: 'Bearer s3cr3t',
          lane: 'coe-world',
          operator: 'claude-code',
        },
      ]);
      expect(data).toEqual({ lane: 'coe-world', path: '甲.md', fingerprint: 'f1', data: 'x' });
    } finally {
      server.stop(true);
    }
  });
});
