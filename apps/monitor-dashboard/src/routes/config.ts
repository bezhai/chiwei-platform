import Router from '@koa/router';

const router = new Router();

router.get('/api/config', async (ctx) => {
  const grafanaHost = process.env.DASHBOARD_GRAFANA_HOST || '';
  const langfuseHost = process.env.DASHBOARD_LANGFUSE_HOST || '';
  const langfuseProjectId = process.env.DASHBOARD_LANGFUSE_PROJECT_ID || '';

  ctx.body = {
    grafanaUrl: grafanaHost,
    kibanaUrl: grafanaHost,
    langfuseUrl: `${langfuseHost}/project/${langfuseProjectId}`,
  };
});

router.get('/api/health', async (ctx) => {
  ctx.body = {
    status: 'ok',
    timestamp: new Date().toISOString(),
    service: 'monitor-dashboard',
    version: process.env.VERSION || process.env.GIT_SHA || 'unknown',
  };
});

export default router;
