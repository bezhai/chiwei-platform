import Router from '@koa/router';
import axios from 'axios';

const router = new Router();

router.get('/api/service-status', async (ctx) => {
  const paasApi = process.env.DASHBOARD_PAAS_API;
  const paasToken = process.env.DASHBOARD_PAAS_TOKEN;

  if (!paasApi || !paasToken) {
    ctx.status = 500;
    ctx.body = { message: 'DASHBOARD_PAAS_API or DASHBOARD_PAAS_TOKEN not configured' };
    return;
  }

  const headers = { 'X-API-Key': paasToken };
  const timeout = 10000;

  const [appsRes, releasesRes] = await Promise.all([
    axios.get(`${paasApi}/api/v1/apps/`, { headers, timeout }),
    axios.get(`${paasApi}/api/v1/releases/`, { headers, timeout }),
  ]);

  ctx.body = {
    apps: appsRes.data?.data ?? appsRes.data,
    releases: releasesRes.data?.data ?? releasesRes.data,
  };
});

export default router;
