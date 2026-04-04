import { Hono } from 'hono';

const app = new Hono();

app.get('/api/config', async (c) => {
  const grafanaHost = process.env.DASHBOARD_GRAFANA_HOST || '';
  const langfuseHost = process.env.DASHBOARD_LANGFUSE_HOST || '';
  const langfuseProjectId = process.env.DASHBOARD_LANGFUSE_PROJECT_ID || '';

  return c.json({
    grafanaUrl: grafanaHost,
    kibanaUrl: grafanaHost,
    langfuseUrl: `${langfuseHost}/project/${langfuseProjectId}`,
  });
});

app.get('/api/health', async (c) => {
  return c.json({
    status: 'ok',
    timestamp: new Date().toISOString(),
    service: 'monitor-dashboard',
    version: process.env.VERSION || process.env.GIT_SHA || 'unknown',
  });
});

export default app;
