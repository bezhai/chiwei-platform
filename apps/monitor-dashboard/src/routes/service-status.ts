import { Hono } from 'hono';
import axios from 'axios';

const app = new Hono();

app.get('/api/service-status', async (c) => {
  const paasApi = process.env.DASHBOARD_PAAS_API;
  const paasToken = process.env.DASHBOARD_PAAS_TOKEN;

  if (!paasApi || !paasToken) {
    return c.json({ message: 'DASHBOARD_PAAS_API or DASHBOARD_PAAS_TOKEN not configured' }, 500);
  }

  const headers = { 'X-API-Key': paasToken };
  const timeout = 10000;

  const [appsRes, releasesRes] = await Promise.all([
    axios.get(`${paasApi}/api/paas/apps/`, { headers, timeout }),
    axios.get(`${paasApi}/api/paas/releases/`, { headers, timeout }),
  ]);

  return c.json({
    apps: appsRes.data?.data ?? appsRes.data,
    releases: releasesRes.data?.data ?? releasesRes.data,
  });
});

export default app;
