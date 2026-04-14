import { Hono } from 'hono';
import { paasClient } from '../paas-client';

const app = new Hono();

/** GET /api/dynamic-config — 列出所有配置 */
app.get('/api/dynamic-config', async (c) => {
  const lane = c.req.query('lane');
  const params: Record<string, string> = {};
  if (lane) params.lane = lane;
  const data = await paasClient.get('/api/paas/dynamic-config/', params);
  return c.json(data);
});

/** GET /api/dynamic-config/resolved — 解析后的配置快照 */
app.get('/api/dynamic-config/resolved', async (c) => {
  const lane = c.req.query('lane') || 'prod';
  const data = await paasClient.get('/api/paas/dynamic-config/resolved', { lane });
  return c.json(data);
});

/** PUT /api/dynamic-config/:key — 设置配置 */
app.put('/api/dynamic-config/:key', async (c) => {
  const key = c.req.param('key');
  const body = await c.req.json();
  const data = await paasClient.put(`/api/paas/dynamic-config/${encodeURIComponent(key)}`, body);
  return c.json(data);
});

/** DELETE /api/dynamic-config/:key — 删除配置 */
app.delete('/api/dynamic-config/:key', async (c) => {
  const key = c.req.param('key');
  const lane = c.req.query('lane');
  const params: Record<string, string> = {};
  if (lane) params.lane = lane;
  const data = await paasClient.del(`/api/paas/dynamic-config/${encodeURIComponent(key)}`, params);
  return c.json(data);
});

export default app;
