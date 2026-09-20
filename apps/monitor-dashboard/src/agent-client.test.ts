import { describe, it, expect, afterEach } from 'bun:test';
import { getAgentConfig, agentClient, createClient } from './paas-client';

// 钉死打 agent-service 那个出站 client 的配置：地址来自 DASHBOARD_AGENT_API，
// 凭据是内网互信那把 INNER_HTTP_SECRET，走 Authorization: Bearer。
// 现有的 paasClient / channelClient 都指着别的服务、用的是 X-API-Key，不能复用。

const saved = {
  api: process.env.DASHBOARD_AGENT_API,
  secret: process.env.INNER_HTTP_SECRET,
  paasApi: process.env.DASHBOARD_PAAS_API,
  paasToken: process.env.DASHBOARD_PAAS_TOKEN,
};

afterEach(() => {
  if (saved.api === undefined) delete process.env.DASHBOARD_AGENT_API;
  else process.env.DASHBOARD_AGENT_API = saved.api;
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
        return Response.json({ lane: 'coe-living', path: 'a.md', fingerprint: 'f1', content: '正文' });
      },
    });

    try {
      process.env.DASHBOARD_AGENT_API = `http://127.0.0.1:${server.port}`;
      process.env.INNER_HTTP_SECRET = 's3cr3t';

      const data = await agentClient.get(
        '/admin/world-documents/document',
        { path: 'a.md' },
        { 'x-ctx-lane': 'coe-living' },
      );

      expect(seen.length).toBe(1);
      expect(seen[0].url).toBe('/admin/world-documents/document?path=a.md');
      expect(seen[0].auth).toBe('Bearer s3cr3t');
      expect(seen[0].lane).toBe('coe-living');
      expect(data).toEqual({ lane: 'coe-living', path: 'a.md', fingerprint: 'f1', content: '正文' });
    } finally {
      server.stop(true);
    }
  });
});

// createClient 默认会把顶层带 data 键的响应拆开只返回 data（那是 paas-engine 的信封
// 口径）。agent-service 不是那个口径：上游哪天加一个 data 字段，lane 会被静默吃掉，
// 而 lane 是整条链路唯一能证明"我改的是哪棵树"的东西。所以 agentClient 直接透传。
describe('agentClient 不拆包', () => {
  it('上游响应顶层带 data 键时，整个对象原样返回，lane 不被吃掉', async () => {
    const upstreamBody = {
      lane: 'coe-living',
      path: '设定/世界底子.md',
      fingerprint: '812f70471934',
      data: { 这是上游自己的一个字段: '不是信封' },
    };
    const server = Bun.serve({ port: 0, fetch: () => Response.json(upstreamBody) });
    try {
      process.env.DASHBOARD_AGENT_API = `http://127.0.0.1:${server.port}`;
      process.env.INNER_HTTP_SECRET = 's3cr3t';

      const got = await agentClient.get('/admin/world-documents/document', { path: 'a.md' });
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
