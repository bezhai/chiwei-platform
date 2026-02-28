import Router from '@koa/router';
import axios from 'axios';

const router = new Router();

router.get('/api/token-stats', async (ctx) => {
  const apiId = process.env.DASHBOARD_TOKEN_STATS_API_ID || '';
  const baseUrl = process.env.DASHBOARD_TOKEN_STATS_BASE_URL;

  if (!baseUrl) {
    ctx.status = 500;
    ctx.body = { message: 'DASHBOARD_TOKEN_STATS_BASE_URL not configured' };
    return;
  }

  const response = await axios.post(
    `${baseUrl}/apiStats/api/user-stats`,
    { apiId },
    { timeout: 10000 }
  );

  ctx.body = response.data;
});

export default router;
